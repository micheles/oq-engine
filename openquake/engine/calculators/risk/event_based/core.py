# Copyright (c) 2010-2013, GEM Foundation.
#
# OpenQuake is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenQuake.  If not, see <http://www.gnu.org/licenses/>.


"""
Core functionality for the classical PSHA risk calculator.
"""

import random
import collections
import numpy
from scipy import interpolate

from django import db

from openquake.hazardlib.geo import mesh
from openquake.risklib import api, scientific

from openquake.engine.calculators.risk import base, hazard_getters
from openquake.engine.db import models
from openquake.engine.utils import tasks
from openquake.engine import logs
from openquake.engine.performance import EnginePerformanceMonitor
from openquake.engine.calculators.base import signal_task_complete


@tasks.oqtask
@base.count_progress_risk('r')
def event_based(job_id, units, containers, params):
    """
    Celery task for the event based risk calculator.

    :param job_id: the id of the current
        :class:`openquake.engine.db.models.OqJob`
    :param dict units:
      A dict of :class:`..base.CalculationUnit` instances keyed by
      loss type string
    :param containers:
      An instance of :class:`..writers.OutputDict` containing
      output container instances (e.g. a LossCurve)
    :param params:
      An instance of :class:`..base.CalcParams` used to compute
      derived outputs
    """

    def profile(name):
        return EnginePerformanceMonitor(
            name, job_id, event_based, tracing=True)

    # Do the job in other functions, such that they can be unit tested
    # without the celery machinery
    event_loss_tables = dict()

    with db.transaction.commit_on_success(using='reslt_writer'):
        for loss_type in units:
            event_loss_tables[loss_type] = do_event_based(
                loss_type, units[loss_type], containers, params, profile)
    num_items = base.get_num_items(units)
    signal_task_complete(job_id=job_id,
                         num_items=num_items,
                         event_loss_tables=event_loss_tables)
event_based.ignore_result = False


def do_event_based(loss_type, units, containers, params, profile):
    """
    See `event_based` for a description of the params

    :returns: the event loss table generated by `units`
    """
    loss_curves = []
    event_loss_table = collections.Counter()

    for unit in units:
        hid = unit.getter.hazard_output_id
        outputs = individual_outputs(loss_type, unit, params, profile)

        if not outputs.assets:
            logs.LOG.info("Exit from task as no asset could be processed")
            return {}

        if params.sites_disagg:
            with profile('disaggregating results'):
                disagg_outputs = disaggregate(outputs, params)
        else:
            disagg_outputs = None

        loss_curves.append(outputs.loss_curves)
        event_loss_table += outputs.event_loss_table

        with profile('saving individual risk'):
            save_individual_outputs(
                loss_type, containers, hid, outputs, disagg_outputs, params)

        if params.insured_losses:
            insured_curves = list(
                insured_losses(
                    loss_type, unit, outputs.assets, outputs.loss_matrix))
            containers.write(
                outputs.assets, insured_curves,
                output_type="loss_curve", insured=True, hazard_output_id=hid,
                loss_type=loss_type)

    # compute mean and quantile outputs
    if len(units) < 2:
        return event_loss_table

    with profile('computing risk statistics'):
        weights = [unit.getter.weight for unit in units]
        stats = statistics(
            outputs.assets, numpy.array(loss_curves).transpose(1, 0, 2, 3),
            weights, params)

    with profile('saving risk statistics'):
        save_statistical_output(
            loss_type, containers, outputs.assets, stats, params)

    return event_loss_table


class UnitOutputs(object):
    """
  Record the results computed in one calculation units.

  :attr assets:
    an iterable over the assets considered by the calculation units

  :attr list loss_matrix:
    a list holding N numpy arrays of dimension R (N = number of assets,
    R = number of ruptures) with the losses associated to each rupture event
    for each asset. The value of R varies with the asset

  :attr list rupture_id_matrix:
    a list where each of the N elements is a list of R database ID of
    :class:`openquake.engine.db.models.Rupture` objects.

  :attr loss_curves:
    a list of N loss curves (where a loss curve is a 2-tuple losses/poes)

  :attr loss_maps:
    a list of P elements holding list of N loss map values where P is the
    number of `conditional_loss_poes`

  :attr dict event_loss_table:
    a mapping between each rupture id to a loss value
    """
    def __init__(self, assets, loss_matrix, rupture_ids,
                 loss_curves, loss_maps, event_loss_table):
        self.assets = assets
        self.loss_matrix = loss_matrix
        self.rupture_ids = rupture_ids
        self.loss_curves = loss_curves
        self.loss_maps = loss_maps
        self.event_loss_table = event_loss_table


def individual_outputs(loss_type, unit, params, profile):
    event_loss_table = collections.Counter()
    with profile('getting hazard'):
        assets, (ground_motion_values, ruptures) = unit.getter()

    with profile('computing losses, loss curves and maps'):
        loss_matrix, curves = unit.calc(ground_motion_values)

        maps = [[scientific.conditional_loss_ratio(losses, poes, poe)
                 for losses, poes in curves]
                for poe in params.conditional_loss_poes]

        for i, asset in enumerate(assets):
            for j, rupture_id in enumerate(ruptures):
                event_loss_table[rupture_id] += (
                    loss_matrix[i][j] * asset.value(loss_type))

    return UnitOutputs(
        assets, loss_matrix, ruptures, curves, maps, event_loss_table)


def save_individual_outputs(
        loss_type, containers, hid, outputs, disagg_outputs, params):
    # loss curves, maps and fractions
    containers.write(
        outputs.assets,
        outputs.loss_curves,
        output_type="loss_curve", hazard_output_id=hid, loss_type=loss_type)

    containers.write_all(
        "poe", params.conditional_loss_poes,
        outputs.loss_maps,
        outputs.assets,
        output_type="loss_map", hazard_output_id=hid, loss_type=loss_type)

    if params.sites_disagg:
        containers.write(
            disagg_outputs.assets_disagg,
            disagg_outputs.magnitude_distance,
            disagg_outputs.fractions,
            output_type="loss_fraction",
            hazard_output_id=hid,
            variable="magnitude_distance",
            loss_type=loss_type)
        containers.write(
            disagg_outputs.assets_disagg,
            disagg_outputs.coordinate, disagg_outputs.fractions,
            output_type="loss_fraction",
            hazard_output_id=hid,
            variable="coordinate",
            loss_type=loss_type)


def insured_losses(loss_type, unit, assets, loss_ratio_matrix):
    for asset, losses in zip(assets, loss_ratio_matrix):
        asset_insured_losses, poes = scientific.event_based(
            scientific.insured_losses(
                losses,
                asset.value(loss_type),
                asset.deductible,
                asset.ins_limit),
            tses=unit.calc.tses,
            time_span=unit.calc.time_span)
        # FIXME(lp). Insured losses are still computed as absolute
        # values.
        yield asset_insured_losses / asset.value(loss_type), poes


class StatisticalOutputs(object):
    """
    The statistical outputs computed by the classical calculator.
    Each attribute is a numpy array with a collection of N outputs,
    where N is the number of assets.

    :attr assets: the assets over which outputs have been computed
    :attr mean_curves: N mean loss curves. A loss curve is a 2-ple losses/poes
    :attr mean_maps: N x P mean map value (P = number of PoEs)
    :attr mean_fractions: N x F mean fraction value (F = number of disagg PoEs)
    :attr quantile_curves: N x Q quantile loss curves (Q = number of quantiles)
    :attr quantile_maps: N x Q x F quantile fractions
    """
    def __init__(self, assets, mean_curves, mean_maps, quantile_curves,
                 quantile_maps):
        self.assets = assets
        self.mean_curves = mean_curves
        self.mean_maps = mean_maps
        self.quantile_curves = quantile_curves
        self.quantile_maps = quantile_maps


def statistics(assets, curve_matrix, weights, params):
    ret = []

    for curves in curve_matrix:
        non_trivial_curves = [(losses, poes)
                              for losses, poes in curves if losses[-1] > 0]
        if not non_trivial_curves:  # no damage. all trivial curves
            loss_ratios, _poes = curves[0]
            curves_poes = [poes for _losses, poes in curves]
        else:  # standard case
            max_losses = [losses[-1]  # we assume non-decreasing losses
                          for losses, _poes in non_trivial_curves]
            reference_curve = non_trivial_curves[numpy.argmax(max_losses)]
            loss_ratios = reference_curve[0]
            curves_poes = [interpolate.interp1d(
                losses, poes, bounds_error=False, fill_value=0)(loss_ratios)
                for losses, poes in curves]
        mean_curve, quantile_curves, mean_maps, quantile_maps = (
            base.asset_statistics(
                loss_ratios, curves_poes,
                params.quantiles, weights, params.conditional_loss_poes))

        ret.append((mean_curve, mean_maps, quantile_curves, quantile_maps))

    (mean_curves, mean_maps, quantile_curves, quantile_maps) = zip(*ret)
    # now all the lists keep N items

    # transpose maps and fractions to have P/F/Q items of N-sized lists
    mean_maps = numpy.array(mean_maps).transpose()
    quantile_curves = numpy.array(quantile_curves).transpose(1, 0, 2, 3)
    quantile_maps = numpy.array(quantile_maps).transpose(2, 1, 0)

    return StatisticalOutputs(
        assets, mean_curves, mean_maps, quantile_curves, quantile_maps)


def save_statistical_output(loss_type, containers, assets, stats, params):
    # mean curves and maps
    containers.write(
        assets, stats.mean_curves,
        output_type="loss_curve", statistics="mean", loss_type=loss_type)

    containers.write_all(
        "poe", params.conditional_loss_poes, stats.mean_maps,
        assets, output_type="loss_map", statistics="mean", loss_type=loss_type)

    # quantile curves and maps
    containers.write_all(
        "quantile", params.quantiles, stats.quantile_curves,
        assets, output_type="loss_curve",
        statistics="quantile", loss_type=loss_type)

    for quantile, maps in zip(params.quantiles, stats.quantile_maps):
        containers.write_all(
            "poe", params.conditional_loss_poes, maps,
            assets, output_type="loss_map",
            statistics="quantile", quantile=quantile, loss_type=loss_type)


class DisaggregationOutputs(object):
    def __init__(self, assets_disagg, magnitude_distance,
                 coordinate, fractions):
        self.assets_disagg = assets_disagg
        self.magnitude_distance = magnitude_distance
        self.coordinate = coordinate
        self.fractions = fractions


def disaggregate(outputs, params):
    """
Compute disaggregation outputs given the individual `outputs` and `params`

    :param outputs:
      an instance of :class:`UnitOutputs`
    :param params:
      an instance of :class:`..base.CalcParams`
    :returns:
      an instance of :class:`DisaggregationOutputs`
"""
    def disaggregate_site(site, loss_ratios, ruptures, params):
        for fraction, rupture_id in zip(loss_ratios, ruptures):

            rupture = models.SESRupture.objects.get(pk=rupture_id)
            s = rupture.surface
            m = mesh.Mesh(numpy.array([site.x]), numpy.array([site.y]), None)

            mag = numpy.floor(rupture.magnitude / params.mag_bin_width)
            dist = numpy.floor(
                s.get_joyner_boore_distance(m))[0] / params.distance_bin_width

            closest_point = iter(s.get_closest_points(m)).next()
            lon = closest_point.longitude / params.coordinate_bin_width
            lat = closest_point.latitude / params.coordinate_bin_width

            yield "%d,%d" % (mag, dist), "%d,%d" % (lon, lat), fraction

    assets_disagg = []
    disagg_matrix = []
    ruptures = outputs.rupture_ids
    for asset, losses in zip(outputs.assets, outputs.loss_matrix):
        if asset.site in params.sites_disagg:
            disagg_matrix.extend(list(
                disaggregate_site(asset.site, losses, ruptures, params)))
            assets_disagg.append(asset)
    if assets_disagg:
        magnitudes, coordinates, fractions = zip(*disagg_matrix)
    else:
        magnitudes, coordinates, fractions = [], [], []

    return DisaggregationOutputs(
        assets_disagg, magnitudes, coordinates, fractions)


class EventBasedRiskCalculator(base.RiskCalculator):
    """
    Probabilistic Event Based PSHA risk calculator. Computes loss
    curves, loss maps, aggregate losses and insured losses for a given
    set of assets.
    """

    #: The core calculation celery task function
    core_calc_task = event_based

    def __init__(self, job):
        super(EventBasedRiskCalculator, self).__init__(job)
        self.event_loss_tables = dict()
        self.rnd = random.Random()
        self.rnd.seed(self.rc.master_seed)

    def task_completed_hook(self, message):
        """
        Updates the event loss table
        """
        for loss_type in base.loss_types(self.risk_models):
            task_loss_table = message['event_loss_tables'][loss_type]
            self.event_loss_tables[loss_type] += task_loss_table

    def validate_hazard(self):
        """
        Check that the given hazard comes from an event based calculation
        """
        super(EventBasedRiskCalculator, self).validate_hazard()
        if self.rc.hazard_calculation:
            if self.rc.hazard_calculation.calculation_mode != "event_based":
                raise RuntimeError(
                    "The provided hazard calculation ID "
                    "is not an event based calculation")
        elif not self.rc.hazard_output.output_type == "gmf":
            raise RuntimeError(
                "The provided hazard output is not a gmf collection")

    def get_taxonomies(self):
        """
        If insured losses are required we check for the presence of
        the deductible and insurance limit
        """
        taxonomies = super(EventBasedRiskCalculator, self).get_taxonomies()

        if (self.rc.insured_losses and
            self.rc.exposure_model.exposuredata_set.filter(
                (db.models.Q(deductible__isnull=True) |
                 db.models.Q(ins_limit__isnull=True))).exists()):
            raise RuntimeError(
                "Deductible or insured limit missing in exposure")

        # FIXME(lp). Validate sites_disagg to ensure non-empty outputs

        return taxonomies

    def post_process(self):
        """
          Compute aggregate loss curves and event loss tables
        """
        with EnginePerformanceMonitor('post processing', self.job.id):

            time_span, tses = self.hazard_times()

            for loss_type, event_loss_table in self.event_loss_tables.items():
                for hazard_output in self.rc.hazard_outputs():
                    gmf_sets = hazard_output.gmfcollection.gmfset_set.all()

                    aggregate_losses = [
                        event_loss_table[rupture.id]
                        for rupture in models.SESRupture.objects.filter(
                            ses__pk__in=[gmf_set.stochastic_event_set_id
                                         for gmf_set in gmf_sets])
                        if rupture.id in event_loss_table]

                    if aggregate_losses:
                        aggregate_loss_losses, aggregate_loss_poes = (
                            scientific.event_based(
                                aggregate_losses, tses=tses,
                                time_span=time_span,
                                curve_resolution=self.rc.loss_curve_resolution
                            ))

                        models.AggregateLossCurveData.objects.create(
                            loss_curve=models.LossCurve.objects.create(
                                aggregate=True, insured=False,
                                hazard_output=hazard_output,
                                loss_type=loss_type,
                                output=models.Output.objects.create_output(
                                    self.job,
                                    "aggregate loss curves. "
                                    "loss_type=%s hazard=%s" % (
                                        loss_type, hazard_output),
                                    "agg_loss_curve")),
                            losses=aggregate_loss_losses,
                            poes=aggregate_loss_poes,
                            average_loss=scientific.average_loss(
                                aggregate_loss_losses, aggregate_loss_poes))

                event_loss_table_output = models.Output.objects.create_output(
                    self.job, "Event Loss Table", "event_loss")

                with db.transaction.commit_on_success(using='reslt_writer'):
                    for rupture_id, aggregate_loss in event_loss_table.items():
                        models.EventLoss.objects.create(
                            output=event_loss_table_output,
                            rupture_id=rupture_id,
                            loss_type=loss_type,
                            aggregate_loss=aggregate_loss)

    def calculation_units(self, loss_type, assets):
        """
        :returns:
          a list of instances of `..base.CalculationUnit` for the given
          `assets` to be run in the celery task
        """

        # assume all assets have the same taxonomy
        taxonomy = assets[0].taxonomy
        risk_model = self.risk_models[taxonomy][loss_type]

        time_span, tses = self.hazard_times()
        return [base.CalculationUnit(
            api.ProbabilisticEventBased(
                risk_model.vulnerability_function,
                curve_resolution=self.rc.loss_curve_resolution,
                time_span=time_span,
                tses=tses,
                seed=self.rnd.randint(0, models.MAX_SINT_32),
                correlation=self.rc.asset_correlation),
            hazard_getters.GroundMotionValuesGetter(
                ho,
                assets,
                self.rc.best_maximum_distance,
                risk_model.imt))
                for ho in self.rc.hazard_outputs()]

    def hazard_times(self):
        """
        Return the hazard investigation time related to the ground
        motion field and the so-called time representative of the
        stochastic event set
        """
        time_span = self.hc.investigation_time
        return time_span, self.hc.ses_per_logic_tree_path * time_span

    @property
    def calculator_parameters(self):
        """
        Calculator specific parameters
        """

        return base.make_calc_params(
            conditional_loss_poes=self.rc.conditional_loss_poes or [],
            quantiles=self.rc.quantile_loss_curves or [],
            insured_losses=self.rc.insured_losses,
            sites_disagg=self.rc.sites_disagg or [],
            mag_bin_width=self.rc.mag_bin_width,
            distance_bin_width=self.rc.distance_bin_width,
            coordinate_bin_width=self.rc.coordinate_bin_width)

    def create_outputs(self, hazard_output):
        """
        Add Insured Curve output containers
        """
        # includes loss curves and loss maps
        outputs = super(EventBasedRiskCalculator, self).create_outputs(
            hazard_output)

        for loss_type in base.loss_types(self.risk_models):
            name = "insured loss curves. type=%s hazard %s" % (
                loss_type, hazard_output),
            if self.rc.insured_losses:
                outputs.set(
                    models.LossCurve.objects.create(
                        insured=True,
                        loss_type=loss_type,
                        hazard_output=hazard_output,
                        output=models.Output.objects.create_output(
                            self.job, name, "loss_curve")))

            if self.rc.sites_disagg:
                name = ("loss fractions. type=%s variable=magnitude_distance "
                        "hazard=%s" % (loss_type, hazard_output))
                outputs.set(
                    models.LossFraction.objects.create(
                        output=models.Output.objects.create_output(
                            self.job, name, "loss_fraction"),
                        hazard_output=hazard_output,
                        loss_type=loss_type,
                        variable="magnitude_distance"))
                name = ("loss fractions. type=%s variable=coordinates "
                        "hazard=%s" % (loss_type, hazard_output))
                outputs.set(models.LossFraction.objects.create(
                    output=models.Output.objects.create_output(
                        self.job, name, "loss_fraction"),
                    hazard_output=hazard_output,
                    loss_type=loss_type,
                    variable="coordinate"))

        return outputs

    def create_statistical_outputs(self):
        for loss_type in base.loss_types(self.risk_models):
            self.event_loss_tables[loss_type] = collections.Counter()
        return super(
            EventBasedRiskCalculator, self).create_statistical_outputs()
