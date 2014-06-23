# -*- coding: utf-8 -*-

# Copyright (c) 2010-2014, GEM Foundation.
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
Tests for python logic tree processor.
"""

import os
import os.path
import unittest

from openquake.commonlib import logictree
from openquake.commonlib.source import \
    NrmlHazardlibConverter, parse_source_model
from openquake.commonlib.general import distinct

from openquake.engine.calculators.hazard.general import make_gsim_lt

from openquake.engine.tests.utils import helpers


class LogicTreeProcessorTestCase(unittest.TestCase):
    def setUp(self):
        # this is an example with number_of_logic_tree_samples = 1
        cfg = helpers.get_data_path('classical_job.ini')
        job = helpers.get_job(cfg)
        self.source_model_lt = logictree.SourceModelLogicTree.from_hc(
            job.hazard_calculation)
        self.gmpe_lt = make_gsim_lt(
            job.hazard_calculation,
            ['Active Shallow Crust', 'Subduction Interface'])

    def test_sample_source_model(self):
        [(sm_name, weight, branch_ids)] = self.source_model_lt
        self.assertEqual(sm_name, 'example-source-model.xml')
        self.assertIsNone(weight)
        self.assertEqual(('b1', 'b5', 'b8'), branch_ids)

    def test_sample_gmpe(self):
        [(value, weight, branch_ids)] = self.gmpe_lt
        self.assertEqual(value,
                         {'Subduction Interface': 'SadighEtAl1997',
                          'Active Shallow Crust': 'ChiouYoungs2008'})
        self.assertIsNone(weight)
        self.assertEqual(('b2', 'b3'), branch_ids)


class LogicTreeProcessorParsePathTestCase(unittest.TestCase):
    def setUp(self):
        cfg = helpers.get_data_path('classical_job.ini')
        job = helpers.get_job(cfg)
        self.uncertainties_applied = []

        def apply_uncertainty(branchset, value, source):
            fingerprint = (branchset.uncertainty_type, value)
            self.uncertainties_applied.append(fingerprint)
        self.original_apply_uncertainty = logictree.BranchSet.apply_uncertainty
        logictree.BranchSet.apply_uncertainty = apply_uncertainty

        self.source_model_lt = logictree.SourceModelLogicTree.from_hc(
            job.hazard_calculation)
        self.gmpe_lt = make_gsim_lt(
            job.hazard_calculation,
            ['Active Shallow Crust', 'Subduction Interface'])

    def tearDown(self):
        logictree.BranchSet.apply_uncertainty = self.original_apply_uncertainty

    def test_parse_source_model_logictree_path(self):
        make_apply_un = self.source_model_lt.make_apply_uncertainties
        make_apply_un(['b1', 'b5', 'b8'])(None)
        self.assertEqual(self.uncertainties_applied,
                         [('maxMagGRRelative', -0.2),
                          ('bGRRelative', -0.1)])
        del self.uncertainties_applied[:]
        make_apply_un(['b1', 'b3', 'b6'])(None)
        self.assertEqual(self.uncertainties_applied,
                         [('maxMagGRRelative', 0.2),
                          ('bGRRelative', 0.1)])
