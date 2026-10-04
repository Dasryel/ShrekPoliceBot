import unittest
from telethon.tl import types
from mtproto_probe import classify_messages

class BatchClassificationTests(unittest.TestCase):
    def test_empty_missing_and_existing_are_distinct(self):
        live = types.Message(id=1, peer_id=types.PeerChannel(1))
        data = classify_messages([1,2,3], [live, types.MessageEmpty(id=2, peer_id=None)])
        self.assertEqual(data, {'existing_ids':[1], 'empty_ids':[2], 'unreturned_ids':[3]})
    def test_unrelated_result_is_ignored(self):
        data = classify_messages([1], [types.MessageEmpty(id=9, peer_id=None)])
        self.assertEqual(data['unreturned_ids'], [1])
        self.assertEqual(data['empty_ids'], [])

if __name__ == '__main__':
    unittest.main()
