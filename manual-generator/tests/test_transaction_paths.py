"""Publication accepts relative projects while rejecting target traversal."""
from pathlib import Path
import sys,tempfile,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from preview_service import _archive_and_publish

class TransactionPathsTests(unittest.TestCase):
    def test_relative_project_publishes_without_repeating_computation(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            project=Path(directory);source=project/'.ready.json'
            source.write_bytes(b'verified result')
            _archive_and_publish(project.relative_to(Path.cwd()),{Path('result.json'):source},set(),[])
            self.assertEqual((project/'result.json').read_bytes(),b'verified result')
            self.assertFalse(source.exists())

    def test_relative_project_does_not_allow_parent_target(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            project=Path(directory);source=project/'.ready.json';source.write_bytes(b'result')
            with self.assertRaisesRegex(ValueError,'escapes'):
                _archive_and_publish(project.relative_to(Path.cwd()),{Path('../outside.json'):source},set(),[])
            self.assertTrue(source.exists())

if __name__=='__main__':unittest.main()
