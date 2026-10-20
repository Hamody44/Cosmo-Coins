import unittest
from unittest.mock import patch

import main


class StartupPublicIPTests(unittest.TestCase):
    def test_lookup_validates_and_returns_ipv4(self):
        with patch("main.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b'{"ip":"203.0.113.8"}'
            result = main._lookup_public_ip(4)

        self.assertEqual(result, "203.0.113.8")
        urlopen.assert_called_once_with("https://api.ipify.org?format=json", timeout=4)

    def test_lookup_rejects_wrong_ip_family(self):
        with patch("main.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b'{"ip":"2001:db8::1"}'
            with self.assertRaises(ValueError):
                main._lookup_public_ip(4)


if __name__ == "__main__":
    unittest.main()
