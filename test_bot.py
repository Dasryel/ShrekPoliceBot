import unittest
from bot import classify
from telethon import types

class DeletionClassificationTests(unittest.TestCase):
    def test_only_explicit_empty_messages_count_as_deleted(self):
        messages = [
            types.MessageEmpty(id=1, peer_id=None),
            types.MessageEmpty(id=3, peer_id=None),
        ]
        self.assertEqual(classify([1, 2], messages), {1})

if __name__ == '__main__':
    unittest.main()
