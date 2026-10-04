import unittest
from bot import APIError, missing

class DeletionClassificationTests(unittest.TestCase):
    def test_only_missing_source_counts(self):
        self.assertTrue(missing(APIError({'description': 'Bad Request: message to forward not found'})))
        for description in ('Forbidden: bot was kicked', 'Bad Request: chat not found', 'Bad Request: message can\'t be forwarded', 'Too Many Requests: retry after 10'):
            self.assertFalse(missing(APIError({'description': description})))

if __name__ == '__main__':
    unittest.main()
