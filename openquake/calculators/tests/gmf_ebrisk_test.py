# -*- coding: utf-8 -*-
# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright (C) 2017 GEM Foundation
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
import sys
import unittest
import numpy
from nose.plugins.attrib import attr
from openquake.calculators.tests import CalculatorTestCase
from openquake.qa_tests_data.gmf_ebrisk import case_1, case_2, case_3
from openquake.qa_tests_data.event_based_risk import case_2 as ebr_2

aae = numpy.testing.assert_almost_equal


class GmfEbRiskTestCase(CalculatorTestCase):
    @attr('qa', 'risk', 'gmf_ebrisk')
    def test_case_1(self):
        self.run_calc(case_1.__file__, 'job_risk.ini', exports='csv')
        num_events = len(self.calc.datastore['agg_loss_table'])
        self.assertEqual(num_events, 10)

    @attr('qa', 'risk', 'gmf_ebrisk')
    def test_case_2(self):
        # case with 3 sites but gmvs only on 2 sites
        self.run_calc(case_2.__file__, 'job.ini', exports='csv')
        alt = self.calc.datastore['agg_loss_table']
        self.assertEqual(len(alt), 3)
        self.assertEqual(set(alt['rlzi']), set([0]))  # single rlzi
        totloss = alt['loss'].sum()
        aae(totloss, 2.2632332)

    @attr('qa', 'risk', 'gmf_ebrisk')
    def test_case_3(self):
        # case with 13 sites, 10 eids, and several 0 values
        if sys.platform == 'darwin':
            raise unittest.SkipTest('MacOSX')
        self.run_calc(case_3.__file__, 'job.ini', exports='csv')
        alt = self.calc.datastore['agg_loss_table']
        self.assertEqual(len(alt), 8)
        self.assertEqual(set(alt['rlzi']), set([0]))  # single rlzi
        totloss = alt['loss'].sum(axis=0)
        aae(totloss, [7717694.])

        # avg_losses-rlzs has shape (A, R, LI)
        avglosses = self.calc.datastore['avg_losses-rlzs'][:, 0, :].sum(axis=0)
        aae(avglosses, [7717694.5])

    @attr('qa', 'risk', 'gmf_ebrisk')
    def test_ebr_2(self):
        self.run_calc(ebr_2.__file__, 'job_ebrisk.ini', exports='csv')
        alt = self.calc.datastore['agg_loss_table']
        self.assertEqual(len(alt), 20)
        self.assertEqual(set(alt['rlzi']), set([0]))  # single rlzi
        totloss = alt['loss'].sum()
        aae(totloss, numpy.float32(20211.566))
