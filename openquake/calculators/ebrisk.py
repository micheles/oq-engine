# -*- coding: utf-8 -*-
# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright (C) 2015-2020 GEM Foundation
#
# OpenQuake is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenQuake. If not, see <http://www.gnu.org/licenses/>.
import logging
import operator
import itertools
from datetime import datetime
import numpy

from openquake.baselib import datastore, hdf5, parallel, general
from openquake.baselib.python3compat import zip
from openquake.risklib import riskmodels
from openquake.risklib.scientific import LossesByAsset
from openquake.risklib.riskinput import (
    cache_epsilons, get_assets_by_taxo, get_output)
from openquake.commonlib import logs
from openquake.calculators import base, event_based, getters
from openquake.calculators.post_risk import PostRiskCalculator

U8 = numpy.uint8
U16 = numpy.uint16
U32 = numpy.uint32
F32 = numpy.float32
F64 = numpy.float64
TWO32 = 2 ** 32
get_n_occ = operator.itemgetter(1)

gmf_info_dt = numpy.dtype([('rup_id', U32), ('task_no', U16),
                           ('nsites', U16), ('gmfbytes', F32), ('dt', F32)])


def calc_risk(gmfs, param, monitor):
    mon_risk = monitor('computing risk', measuremem=False)
    mon_agg = monitor('aggregating losses', measuremem=False)
    eids = numpy.unique(gmfs['eid'])
    dstore = datastore.read(param['hdf5path'])
    with monitor('getting assets'):
        assets_df = dstore.read_df('assetcol/array', 'site_id')
        exposed_values = dstore['exposed_values/agg'][()]
    with monitor('getting crmodel'):
        crmodel = riskmodels.CompositeRiskModel.read(dstore)
        events = dstore['events'][list(eids)]
        weights = dstore['weights'][()]
    E = len(eids)
    L = len(param['lba'].loss_names)
    elt_dt = [('event_id', U32), ('rlzi', U16), ('loss', (F32, (L,)))]
    alt = general.AccumDict(accum=general.AccumDict(accum=numpy.zeros(L, F32)))
    # aggkey -> eid -> loss
    arr = numpy.zeros((E, L), F32)
    acc = dict(events_per_sid=0, numlosses=numpy.zeros(2, int))  # (kept, tot)
    lba = param['lba']
    tempname = param['tempname']
    eid2rlz = dict(events[['id', 'rlz_id']])
    eid2idx = {eid: idx for idx, eid in enumerate(eids)}
    aggby = param['aggregate_by']

    minimum_loss = []
    fraction = param['minimum_loss_fraction'] / len(assets_df)
    for lt, lti in crmodel.lti.items():
        val = exposed_values[lti] * fraction
        minimum_loss.append(val)
        if lt in lba.policy_dict:  # same order as in lba.compute
            minimum_loss.append(val)

    haz_by_sid = general.group_array(gmfs, 'sid')
    for sid, assets_on_sid in assets_df.groupby('site_id'):
        assets = assets_on_sid.to_records()
        if assets.shape == ():  # hack
            assets = numpy.array([assets])
        try:
            haz = haz_by_sid[sid]
        except KeyError:  # no hazard here
            continue
        with mon_risk:
            acc['events_per_sid'] += len(haz)
            if param['avg_losses']:
                ws = weights[[eid2rlz[eid] for eid in haz['eid']]]
            assets_by_taxo = get_assets_by_taxo(assets_on_sid, tempname)
            eidx = numpy.array([eid2idx[eid] for eid in haz['eid']])
            out = get_output(crmodel, assets_by_taxo, haz)
        for lti, lt in enumerate(crmodel.loss_types):
            lratios = out[lt]
            if lt == 'occupants':
                field = 'occupants_None'
            else:
                field = 'value-' + lt
            for a, asset in enumerate(assets):
                if aggby:
                    idx = ','.join(map(str, asset[aggby]))
                aid = asset['ordinal']
                ls = asset[field] * lratios[a]
                for loss_idx, losses in lba.compute(asset, ls, lt):
                    kept = 0
                    with mon_agg:
                        if aggby:
                            for loss, eid in zip(losses, out.eids):
                                if loss >= minimum_loss[loss_idx]:
                                    alt[idx][eid][loss_idx] += loss
                                    kept += 1
                        arr[eidx, loss_idx] += losses
                    if param['avg_losses']:  # this is really fast
                        lba.losses_by_A[aid, loss_idx] += losses @ ws
                    acc['numlosses'] += numpy.array([kept, len(losses)])
    if len(gmfs):
        acc['events_per_sid'] /= len(gmfs)
    acc['elt'] = numpy.fromiter(  # this is ultra-fast
        ((event['id'], event['rlz_id'], losses)
         for event, losses in zip(events, arr) if losses.sum()), elt_dt)
    acc['alt'] = {idx: numpy.fromiter(  # already sorted by aid
        ((eid, eid2rlz[eid], loss) for eid, loss in alt[idx].items()),
        elt_dt) for idx in alt}
    if param['avg_losses']:
        acc['losses_by_A'] = param['lba'].losses_by_A * param['ses_ratio']
        # without resetting the cache the sequential avg_losses would be wrong!
        del param['lba'].__dict__['losses_by_A']
    return acc


def ebrisk(rupgetter, srcfilter, param, monitor):
    """
    :param rupgetter: RuptureGetter with multiple ruptures
    :param srcfilter: a SourceFilter
    :param param: dictionary of parameters coming from oqparam
    :param monitor: a Monitor instance
    :returns: a dictionary with keys elt, alt, ...
    """
    mon_rup = monitor('getting ruptures', measuremem=False)
    mon_haz = monitor('getting hazard', measuremem=False)
    gmfs = []
    gmf_info = []
    gg = getters.GmfGetter(rupgetter, srcfilter, param['oqparam'])
    nbytes = 0
    for c in gg.gen_computers(mon_rup):
        with mon_haz:
            data, time_by_rup = c.compute_all(gg.min_iml, gg.rlzs_by_gsim)
        if len(data):
            gmfs.append(data)
            nbytes += data.nbytes
        gmf_info.append((c.rupture.id, mon_haz.task_no, len(c.sids),
                         data.nbytes, mon_haz.dt))
        if nbytes > param['ebrisk_maxsize']:
            msg = 'produced subtask'
            try:
                logs.dbcmd('log', monitor.calc_id, datetime.utcnow(), 'DEBUG',
                           'ebrisk#%d' % monitor.task_no, msg)
            except Exception:  # for `oq run`
                print(msg)
            yield calc_risk, numpy.concatenate(gmfs), param
            nbytes = 0
            gmfs = []
    res = {}
    if gmfs:
        res.update(calc_risk(numpy.concatenate(gmfs), param, monitor))
    if gmf_info:
        res['gmf_info'] = numpy.array(gmf_info, gmf_info_dt)
    yield res


def gen_indices(tagcol, aggby):
    ranges = [range(1, len(getattr(tagcol, tagname))) for tagname in aggby]
    for idxs in itertools.product(*ranges):
        yield idxs


@base.calculators.add('ebrisk')
class EbriskCalculator(event_based.EventBasedCalculator):
    """
    Event based PSHA calculator generating event loss tables
    """
    core_task = ebrisk
    is_stochastic = True
    precalc = 'event_based'
    accept_precalc = ['event_based', 'event_based_risk', 'ucerf_hazard']

    def pre_execute(self):
        oq = self.oqparam
        oq.ground_motion_fields = False
        super().pre_execute()
        self.param['lba'] = lba = (
            LossesByAsset(self.assetcol, oq.loss_names,
                          self.policy_name, self.policy_dict))
        self.param['ses_ratio'] = oq.ses_ratio
        self.param['aggregate_by'] = oq.aggregate_by
        self.param['ebrisk_maxsize'] = oq.ebrisk_maxsize
        self.param['minimum_loss_fraction'] = oq.minimum_loss_fraction

        self.A = A = len(self.assetcol)
        self.L = L = len(lba.loss_names)
        elt_dt = [('event_id', U32), ('rlzi', U16), ('loss', (F32, (L,)))]
        for idxs in gen_indices(self.assetcol.tagcol, oq.aggregate_by):
            idx = ','.join(map(str, idxs))
            self.datastore.create_dset('event_loss_table/' + idx, elt_dt)
        self.param.pop('oqparam', None)  # unneeded
        A = len(self.assetcol)
        self.datastore.create_dset('avg_losses-stats', F32, (A, 1, L))  # mean
        elt_nbytes = 4 * self.E * L
        if elt_nbytes / (oq.concurrent_tasks or 1) > TWO32:
            raise RuntimeError('The event loss table is too big to be transfer'
                               'red with %d tasks' % oq.concurrent_tasks)
        self.datastore.create_dset('losses_by_event', elt_dt)
        self.datastore.create_dset('gmf_info', gmf_info_dt)

    def execute(self):
        self.datastore.flush()  # just to be sure
        oq = self.oqparam
        parent = self.datastore.parent
        csm_info = parent['csm_info'] if parent else self.csm_info
        self.init_logic_tree(csm_info)
        self.set_param(
            hdf5path=self.datastore.filename,
            tempname=cache_epsilons(
                self.datastore, oq, self.assetcol, self.crmodel, self.E))
        srcfilter = self.src_filter(self.datastore.tempname)
        logging.info('Sending %d ruptures', len(self.datastore['ruptures']))
        self.events_per_sid = []
        self.numlosses = 0
        self.datastore.swmr_on()
        self.indices = general.AccumDict(accum=[])  # rlzi -> [(start, stop)]
        smap = parallel.Starmap(
            self.core_task.__func__, h5=self.datastore.hdf5)
        for rgetter in getters.gen_rupture_getters(
                self.datastore, srcfilter=srcfilter):
            smap.submit((rgetter, srcfilter, self.param))
        smap.reduce(self.agg_dicts)
        if self.indices:
            self.datastore['event_loss_table/indices'] = self.indices
        gmf_bytes = self.datastore['gmf_info']['gmfbytes'].sum()
        logging.info(
            'Produced %s of GMFs', general.humansize(gmf_bytes))
        logging.info('Considered {:_d} / {:_d} losses'.format(*self.numlosses))
        return 1

    def agg_dicts(self, dummy, dic):
        """
        :param dummy: unused parameter
        :param dic: dictionary with keys elt, losses_by_A
        """
        if 'gmf_info' in dic:
            hdf5.extend(self.datastore['gmf_info'], dic.pop('gmf_info'))
        if not dic:
            return
        self.oqparam.ground_motion_fields = False  # hack
        with self.monitor('saving losses_by_event and event_loss_table'):
            hdf5.extend(self.datastore['losses_by_event'], dic['elt'])
            for idx, arr in dic['alt'].items():
                hdf5.extend(self.datastore['event_loss_table/' + idx], arr)
        if self.oqparam.avg_losses:
            with self.monitor('saving avg_losses'):
                self.datastore['avg_losses-stats'][:, 0] += dic['losses_by_A']
        self.events_per_sid.append(dic['events_per_sid'])
        self.numlosses += dic['numlosses']

    def post_execute(self, dummy):
        """
        Compute and store average losses from the losses_by_event dataset,
        and then loss curves and maps.
        """
        oq = self.oqparam
        if oq.avg_losses:
            self.datastore['avg_losses-stats'].attrs['stats'] = [b'mean']
        prc = PostRiskCalculator(oq, self.datastore.calc_id)
        prc.datastore.parent = self.datastore.parent
        prc.run()
