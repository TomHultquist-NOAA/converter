import unittest, tempfile, pathlib, sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
import spc_lsr as spc

class SPCCollectorTests(unittest.TestCase):
    def test_boundary_and_hazards(self):
        csv='''Time,F_Scale,Location,County,State,Lat,Lon,Comments
1159,UNK,North,ABC,OK,35.2,-97.4,Early tornado
1200,UNK,South,ABC,OK,35.1,-97.3,Afternoon tornado
Time,Speed,Location,County,State,Lat,Lon,Comments
0000,60,Town,ABC,KS,39.1,-99.8,Measured wind
Time,Size,Location,County,State,Lat,Lon,Comments
2359,175,Field,ABC,TX,32.9,-99.2,Large hail
'''
        result=spc.parse_csv(csv,'261029')
        self.assertEqual(len(result),4)
        bytype={r['properties']['type']:r for r in result if r['properties']['type']!='tornado'}
        self.assertEqual(bytype['wind']['properties']['time'],'2026-10-30T00:00:00Z')
        self.assertEqual(bytype['hail']['properties']['time'],'2026-10-29T23:59:00Z')
        ts=[r['properties']['time'] for r in result if r['properties']['type']=='tornado']
        self.assertIn('2026-10-30T11:59:00Z',ts)
        self.assertIn('2026-10-29T12:00:00Z',ts)
    def test_invalid_duplicate(self):
        csv='''Time,Speed,Location,County,State,Lat,Lon,Comments
1300,UNK,X,Y,OK,0,0,Outside bounds
1301,UNK,X,Y,OK,34,-97,Valid
1301,UNK,X,Y,OK,34,-97,Duplicated
'''
        a=spc.parse_csv(csv,'261029');self.assertEqual(len(a),1)
    def test_dryrun(self):
        with tempfile.TemporaryDirectory() as d:
            result=spc.main(['--start-day','261028','--end-day','261029','--output-root',d,'--dry-run'])
            self.assertEqual(result,0)
            self.assertEqual(list(pathlib.Path(d).rglob('*.json')),[])

if __name__=='__main__':unittest.main()
