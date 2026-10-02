"""utils/out_path.normalize_out_path: trailing separators go, drive roots stay roots."""
import pytest

from utils.out_path import normalize_out_path


@pytest.mark.parametrize('given, expected', [
    ('E:\\Music', 'E:\\Music'),
    ('E:\\Music\\', 'E:\\Music'),
    ('E:/Music/', 'E:/Music'),
    ('Z:\\', 'Z:\\'),              # drive root stays the root
    ('Z:', 'Z:\\'),                # bare drive = "current dir on Z:" -> made the root
    ('z:/', 'z:\\'),
    ('Z:"', 'Z:\\'),               # cmd: -o "Z:\" arrives as  Z:"
    ('E:\\Music"', 'E:\\Music'),
    ('  E:\\Music\\  ', 'E:\\Music'),
    ('\\\\NAS\\music\\', '\\\\NAS\\music'),
    ('/mnt/music/', '/mnt/music'),
    ('/', '/'),
    ('', ''),
    (None, None),
])
def test_normalize_out_path(given, expected):
    assert normalize_out_path(given) == expected
