"""NOHRSC unit detection tests: never guess the source snowfall depth units."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import verification_convert as convert


class NohrscUnitTests(unittest.TestCase):
    def test_supported_units_in_verbose_description(self):
        samples={'m':1., 'mm':.001, 'cm':.01, 'in':.0254, 'inches':.0254}
        for unit,factor in samples.items():
            with self.subTest(unit=unit):
                raw=f'1:0:vt=2026103000:surface:anl:SNOW Snowfall [{unit}]:\n    ndata=1:min=0:max=1'
                got=convert.nohrsc_depth_unit_from_verbose(raw)
                self.assertAlmostEqual(convert.NOHRSC_DEPTH_METRE_FACTORS[got],factor)

    def test_asnow_description_and_alternate_key(self):
        self.assertEqual(convert.nohrsc_depth_unit_from_verbose(
            '1:0:vt=2026103000:surface:anl:ASNOW Total snowfall [m]:'),'m')
        self.assertEqual(convert.nohrsc_depth_unit_from_verbose(
            '1:0:vt=2026103000:surface:anl:SNOW:\n    units = mm'),'mm')

    def test_unknown_or_incompatible_units_fail_closed(self):
        for raw in ['1:0:vt=2026103000:surface:anl:SNOW Snowfall:',
                    '1:0:vt=2026103000:surface:anl:SNOW Snowfall [kg/m2]:',
                    '1:0:vt=2026103000:surface:anl:SNOW Snowfall [%]:',
                    '1:0:vt=2026103000:surface:anl:SNOW Snowfall [furlongs]:',
                    '1:0:vt=2026103000:surface:anl:TMP Temperature [K]:']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                convert.nohrsc_depth_unit_from_verbose(raw)

    def test_no_accidental_metadata_unit_matching(self):
        sample='1:0:vt=2026103000:surface:anl:SNOW Snowfall [kg/m2]:\n grid label [m]'
        with self.assertRaises(ValueError):convert.nohrsc_depth_unit_from_verbose(sample)

if __name__=='__main__':unittest.main()
