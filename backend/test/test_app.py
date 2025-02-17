import unittest
import os
import io
import base64
import pandas as pd
import json
from chalice.config import Config
from chalice.local import LocalGateway
from app import app, s3, send_write_request, dynamodb
from requests_toolbelt import MultipartEncoder
from pandas.util.testing import assert_frame_equal
import hashlib

class TestApp(unittest.TestCase):

    def setUp(self):
        self.lg = LocalGateway(app, Config())

    def test_upload(self):
        payload = self.get_pdf_payload()
        response = self.lg.handle_request(method='POST', path='/upload',
                headers={'Content-Type': payload.content_type}, body=payload.to_string())

        self.assertEqual(response['statusCode'], 200)
        self.assertEqual(response['body'], self.get_fixture_content('uob.csv'))
        self.check_and_cleanup_pdf()

    def test_upload_json(self):
        payload = self.get_pdf_payload()
        response = self.lg.handle_request(method='POST', path='/upload',
                headers={'Content-Type': payload.content_type,
                         'Accept': 'application/json'},
                body=payload.to_string())

        self.assertEqual(response['statusCode'], 200)
        self.assertEqual(response['body'], self.get_fixture_content('uob.json').strip())
        self.check_and_cleanup_pdf()

    def test_upload_with_password(self):
        payload = self.get_pdf_payload('123abc')
        response = self.lg.handle_request(method='POST', path='/upload',
                headers={'Content-Type': payload.content_type}, body=payload.to_string())

        self.assertEqual(response['statusCode'], 200)
        self.assertEqual(response['body'], self.get_fixture_content('uob.csv'))
        self.check_and_cleanup_pdf()

    def test_upload_bad_request(self):
        response = self.lg.handle_request(method='POST', path='/upload',
                headers={'Content-Type': 'multipart/form-data'}, body='')
        self.assertEqual(response['statusCode'], 400)
        self.assertEqual(response['body'], 'Missing form data')

        payload = MultipartEncoder({'password': '123'})
        response = self.lg.handle_request(method='POST', path='/upload',
                headers={'Content-Type': payload.content_type}, body=payload.to_string())
        self.assertEqual(response['statusCode'], 400)
        self.assertEqual(response['body'], 'Missing upload file')

    def test_confirm_and_transactions(self):
        get_headers = {}
        post_headers = {'Content-Type': 'text/csv'}
        parse_fn = lambda body: pd.read_csv(body, index_col=False)
        self.verify_confirm_and_transactions(get_headers, post_headers, 'uob.csv', parse_fn)

    def test_confirm_and_transactions_json(self):
        get_headers = {'Accept': 'application/json'}
        post_headers = {'Content-Type': 'application/json'}
        parse_fn = lambda body: pd.read_json(body,
                orient='records', convert_dates=False)
        self.verify_confirm_and_transactions(get_headers, post_headers, 'uob.json', parse_fn)

    def test_delete_transaction(self):
        # create new transactions
        payload = self.get_fixture_content('uob.json')
        response = self.lg.handle_request(method='POST', path='/confirm',
                headers={'Content-Type': 'application/json'}, body=payload)
        self.assertEqual(response['statusCode'], 201)

        # get transactions with IDs
        response = self.lg.handle_request(method='GET',
                path='/transactions?txid=1',
                headers={'Accept': 'application/json'}, body='')
        self.assertEqual(response['statusCode'], 200)
        transactions = json.loads(response['body'])
        # delete the first two transactions
        txids_to_delete = [i['txid'] for i in transactions[0:2]]
        self.assertEqual(len(txids_to_delete), 2)
        for txid in txids_to_delete:
            response = self.lg.handle_request(method='DELETE',
                    path=f'/transactions/{txid}',
                    headers={}, body='')
            self.assertEqual(response['statusCode'], 200)

        response = self.lg.handle_request(method='GET',
                path='/transactions?txid=1',
                headers={'Accept': 'application/json'}, body='')
        self.assertEqual(response['statusCode'], 200)
        transactions_after_delete = json.loads(response['body'])
        self.assertEqual(len(transactions_after_delete), len(transactions) - 2)
        remaining_txids = [i['txid'] for i in transactions_after_delete]
        for txid in txids_to_delete:
            self.assertFalse(txid in remaining_txids)

    def test_delete_invalid_transaction(self):
        response = self.lg.handle_request(method='GET',
                path='/transactions?txid=1',
                headers={'Accept': 'application/json'}, body='')
        self.assertEqual(response['statusCode'], 200)
        transactions = json.loads(response['body'])

        response = self.lg.handle_request(method='DELETE',
                path=f'/transactions/foobar',
                headers={}, body='')
        self.assertEqual(response['statusCode'], 400)

        response = self.lg.handle_request(method='GET',
                path='/transactions?txid=1',
                headers={'Accept': 'application/json'}, body='')
        self.assertEqual(response['statusCode'], 200)
        transactions_after_delete = json.loads(response['body'])
        self.assertEqual(len(transactions_after_delete), len(transactions))

    def verify_confirm_and_transactions(self,
            get_request_headers, post_request_headers,
            request_payload_filename, response_body_parse_fn):
        self.delete_all_tx_of_test_user(get_request_headers, response_body_parse_fn)

        # create new transactions
        payload = self.get_fixture_content(request_payload_filename)
        response = self.lg.handle_request(method='POST', path='/confirm',
                headers=post_request_headers, body=payload)
        self.assertEqual(response['statusCode'], 201)

        # verify transactions created
        response = self.lg.handle_request(method='GET', path='/transactions',
                headers=get_request_headers, body='')
        self.assertEqual(response['statusCode'], 200)
        self.assert_str_as_dataframe_equal(io.StringIO(response['body']),
                self.get_fixture_path(request_payload_filename),
                response_body_parse_fn)

    def test_confirm_invalid_payload(self):
        # missing payload
        response = self.lg.handle_request(method='POST', path='/confirm',
                headers={}, body='')
        self.assertEqual(response['statusCode'], 400)

        # mismatched content type
        response = self.lg.handle_request(method='POST', path='/confirm',
                headers={'Content-Type': 'text/csv'},
                body=self.get_fixture_content('uob.json'))
        self.assertEqual(response['statusCode'], 400)

    def test_update(self):
        self.delete_all_tx_of_test_user()

        confirm_payload = '''
date,description,amount,statement_date,category
2016-07-13,10 JUL CR INTEREST,-40.59,2016-08-12,Interest Income
2016-07-18,15 JUL CR INTEREST,-72.97,2016-08-12,Interest Income
2016-07-15,13 JUL GRAIN - GO106604 SINGAPORE,45.75,2016-08-12,Delivery
2016-07-18,CGH CLINICS $110.12 001/003,75,2016-08-12,Dentist
2016-07-18,CGH CLINICS $110.12 002/003,75,2016-08-12,Dentist
2016-07-18,CGH CLINICS $110.12 003/003,75,2016-08-12,Dentist
'''

        response = self.lg.handle_request(method='POST', path='/confirm',
                headers={'Content-type': 'text/csv'}, body=confirm_payload)
        self.assertEqual(response['statusCode'], 201)

        update_payload = json.dumps({
            'description': '10 JUL CR INTEREST',
            'category': 'Returned Purchase'})

        response = self.lg.handle_request(method='POST', path='/update',
                headers={'Content-type': 'application/json'}, body=update_payload)
        self.assertEqual(response['statusCode'], 200)
        self.assertEqual(
                json.loads(response['body']),
                ["2016-07-13-3be9c2085941beac69ffd47410d30ad9805dc6fe-0000",
                 "2016-07-18-3be9c2085941beac69ffd47410d30ad9805dc6fe-0001"]
        )

        update_payload = json.dumps({
            'description': 'CGH CLINICS $110.12 003/003',
            'category': 'Doctor'})

        response = self.lg.handle_request(method='POST', path='/update',
                headers={'Content-type': 'application/json'}, body=update_payload)
        self.assertEqual(response['statusCode'], 200)
        self.assertEqual(
                json.loads(response['body']),
                ["2016-07-18-3be9c2085941beac69ffd47410d30ad9805dc6fe-0003",
                 "2016-07-18-3be9c2085941beac69ffd47410d30ad9805dc6fe-0004",
                 "2016-07-18-3be9c2085941beac69ffd47410d30ad9805dc6fe-0005"]
        )

        expected_payload = '''
date,description,amount,statement_date,category
2016-07-13,10 JUL CR INTEREST,-40.59,2016-08-12,Returned Purchase
2016-07-18,15 JUL CR INTEREST,-72.97,2016-08-12,Returned Purchase
2016-07-15,13 JUL GRAIN - GO106604 SINGAPORE,45.75,2016-08-12,Delivery
2016-07-18,CGH CLINICS $110.12 001/003,75,2016-08-12,Doctor
2016-07-18,CGH CLINICS $110.12 002/003,75,2016-08-12,Doctor
2016-07-18,CGH CLINICS $110.12 003/003,75,2016-08-12,Doctor
'''
        response = self.lg.handle_request(method='GET', path='/transactions',
                headers={}, body='')
        self.assert_str_as_dataframe_equal(io.StringIO(response['body']),
                                           io.StringIO(expected_payload))

    def test_update_invalid_payload(self):
        response = self.lg.handle_request(method='POST', path='/update',
                headers={'Content-type': 'application/json'}, body='')
        self.assertEqual(response['statusCode'], 400)

        incomplete_update_payload = json.dumps({'description': 'foo'})
        response = self.lg.handle_request(method='POST', path='/update',
                headers={'Content-type': 'application/json'},
                body=incomplete_update_payload)
        self.assertEqual(response['statusCode'], 400)

    def test_request(self):
        password = '123abc'
        payload = self.get_pdf_payload(password)
        response = self.lg.handle_request(method='POST', path='/request',
                headers={'Content-Type': payload.content_type}, body=payload.to_string())

        self.assertEqual(response['statusCode'], 201)
        expected_tags = [{'Key': 'password', 'Value': password},
                         {'Key': 'uuid', 'Value': 'wei'}]
        self.check_and_cleanup_pdf(bucket='moneycat-request-pdfs-dev', expected_tags=expected_tags)

    def test_categories(self):
        response = self.lg.handle_request(method='GET', path='/categories',
                headers={}, body='')

        self.assertEqual(response['statusCode'], 200)
        self.assertEqual(response['body'],
                         self.get_fixture_content('categories.json'))

    def test_categories_etag_cache_header(self):
        response = self.lg.handle_request(method='GET', path='/categories',
                headers={}, body='')

        expected_body = self.get_fixture_content('categories.json')
        expected_etag = hashlib.md5(expected_body.encode('utf-8')).hexdigest()
        self.assertEqual(response['headers']['ETag'], expected_etag)

        response = self.lg.handle_request(method='GET', path='/categories',
                headers={'If-None-Match': expected_etag}, body='')
        self.assertEqual(response['statusCode'], 304)

    #### helper functions ###
    def get_pdf_payload(self, password=None):
        args = {'file': ('uob.pdf', self.get_pdf_data(), 'application/pdf')}
        if password:
            args['password'] = password
        return MultipartEncoder(args)

    def check_and_cleanup_pdf(self, bucket='moneycat-pdfs-dev', expected_tags=None):
        # check that pdf is saved on s3
        pdfs = s3.list_objects(Bucket=bucket)['Contents']
        latest_pdf = sorted(pdfs, key=lambda k: k['LastModified'])[-1]
        pdf_obj = s3.get_object(Bucket=bucket, Key=latest_pdf['Key'])
        self.assertEqual(pdf_obj['Body'].read(), self.get_pdf_data())

        if expected_tags:
            self.assertEqual(pdf_obj['TagCount'], len(expected_tags))
            tags = s3.get_object_tagging(Bucket=bucket, Key=latest_pdf['Key'])['TagSet']
            self.assertEqual(tags, expected_tags)

        # only clean up if test passes, when it fails save pdf for debugging
        s3.delete_object(Bucket=bucket, Key=latest_pdf['Key'])

    def read_and_close(self, filename, mode='r'):
        with open(filename, mode) as f:
            return f.read()

    def get_fixture_path(self, filename):
        return os.path.join(os.path.dirname(__file__), 'data', filename)

    def get_fixture_content(self, filename, mode='r'):
        return self.read_and_close(self.get_fixture_path(filename), mode)

    def get_pdf_data(self):
        return self.get_fixture_content('uob.pdf', 'rb')

    def delete_all_tx_of_test_user(self, get_request_headers={},
            response_body_parse_fn=None):
        response = self.lg.handle_request(method='GET', path='/transactions?txid=1',
                headers=get_request_headers, body='')
        self.assertEqual(response['statusCode'], 200)

        body = response['body']
        if not body or not body.strip():
            print('No data to delete')
            return
        if not response_body_parse_fn:
            response_body_parse_fn = lambda body: pd.read_csv(body, index_col=False)
        tx_df = response_body_parse_fn(io.StringIO(body))
        if tx_df.empty:
            print('No data to delete')
            return

        requests = []
        for index, row in tx_df.iterrows():
            item = {"uuid": {"S": "wei"},
                    "txid": {"S": row['txid']}}
            requests.append({"DeleteRequest": {"Key": item}})
            if len(requests) == 25:
                send_write_request(requests)
                requests = [] # reset requests buffer for next batch
        if len(requests) > 0:
            send_write_request(requests)

    def assert_str_as_dataframe_equal(self, actual_str_io, expected_str_io,
                                      str_parse_fn=None):
        if not str_parse_fn:
            str_parse_fn = lambda body: pd.read_csv(body, index_col=False)
        expected_df = str_parse_fn(expected_str_io) \
                .sort_values(['date', 'description', 'amount']) \
                .reset_index(drop=True)
        if 'foreign_amount' in expected_df:
            expected_df.drop(columns=['foreign_amount'], inplace=True)
        df = str_parse_fn(actual_str_io) \
                .sort_values(['date', 'description', 'amount']) \
                .reset_index(drop=True)
        assert_frame_equal(df, expected_df, check_like=True)


if __name__ == '__main__':
    unittest.main()

