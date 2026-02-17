@IsTest
private class AccountS3ExportBatchTest {
    @IsTest
    static void exportsAccountsInChunksAndUploadsManifest() {
        insert buildAccounts(5);

        RecordingS3Mock.reset(false);
        Test.setMock(HttpCalloutMock.class, new RecordingS3Mock());

        Test.startTest();
        Database.executeBatch(
            new AccountS3ExportBatch(
                'test-client-id',
                'test-client-secret',
                'us-east-1',
                'my-test-bucket',
                'exports/accounts'
            ),
            2
        );
        Test.stopTest();

        System.assertEquals(4, RecordingS3Mock.requestCount, 'Expected 3 CSV uploads + 1 manifest upload.');
        System.assertEquals(3, RecordingS3Mock.csvUploadCount, 'Unexpected CSV upload count.');
        System.assertEquals(1, RecordingS3Mock.manifestUploadCount, 'Unexpected manifest upload count.');
        System.assertEquals('PUT', RecordingS3Mock.lastMethod, 'S3 upload should use PUT.');
        System.assert(
            RecordingS3Mock.lastAuthorizationHeader != null
            && RecordingS3Mock.lastAuthorizationHeader.startsWith('AWS4-HMAC-SHA256'),
            'Expected SigV4 Authorization header.'
        );
        System.assert(RecordingS3Mock.sawCsvHeader, 'Expected CSV header in uploaded payload.');
    }

    @IsTest
    static void retriesOnTransientFailure() {
        insert buildAccounts(1);

        RecordingS3Mock.reset(true);
        Test.setMock(HttpCalloutMock.class, new RecordingS3Mock());

        Test.startTest();
        Database.executeBatch(
            new AccountS3ExportBatch(
                'test-client-id',
                'test-client-secret',
                'us-east-1',
                'my-test-bucket',
                'exports/accounts'
            ),
            1
        );
        Test.stopTest();

        System.assertEquals(3, RecordingS3Mock.requestCount, 'Expected one retried CSV request and one manifest upload.');
        System.assertEquals(2, RecordingS3Mock.csvUploadCount, 'Expected the CSV upload to be attempted twice.');
        System.assertEquals(1, RecordingS3Mock.manifestUploadCount, 'Expected a single manifest upload.');
    }

    @IsTest
    static void uploadsHeaderOnlyWhenNoAccountsExist() {
        RecordingS3Mock.reset(false);
        Test.setMock(HttpCalloutMock.class, new RecordingS3Mock());

        Test.startTest();
        Database.executeBatch(
            new AccountS3ExportBatch(
                'test-client-id',
                'test-client-secret',
                'us-east-1',
                'my-test-bucket',
                'exports/accounts'
            ),
            200
        );
        Test.stopTest();

        System.assertEquals(2, RecordingS3Mock.requestCount, 'Expected empty CSV + manifest uploads.');
        System.assertEquals(1, RecordingS3Mock.csvUploadCount, 'Expected one CSV upload for empty export.');
        System.assertEquals(1, RecordingS3Mock.manifestUploadCount, 'Expected one manifest upload.');
    }

    private static List<Account> buildAccounts(Integer howMany) {
        List<Account> accounts = new List<Account>();
        for (Integer i = 0; i < howMany; i++) {
            accounts.add(
                new Account(
                    Name = 'S3 Export Test Account ' + i,
                    AccountNumber = 'ACC-' + i,
                    Industry = 'Technology'
                )
            );
        }
        return accounts;
    }

    private class RecordingS3Mock implements HttpCalloutMock {
        public static Integer requestCount = 0;
        public static Integer csvUploadCount = 0;
        public static Integer manifestUploadCount = 0;
        public static Integer csvFailureCount = 0;
        public static Boolean failFirstCsvUpload = false;
        public static Boolean sawCsvHeader = false;
        public static String lastAuthorizationHeader;
        public static String lastMethod;

        public static void reset(Boolean shouldFailFirstCsvUpload) {
            requestCount = 0;
            csvUploadCount = 0;
            manifestUploadCount = 0;
            csvFailureCount = 0;
            failFirstCsvUpload = shouldFailFirstCsvUpload;
            sawCsvHeader = false;
            lastAuthorizationHeader = null;
            lastMethod = null;
        }

        public HttpResponse respond(HttpRequest request) {
            requestCount++;
            lastAuthorizationHeader = request.getHeader('Authorization');
            lastMethod = request.getMethod();

            String endpoint = request.getEndpoint();
            Boolean isManifest = endpoint != null && endpoint.contains('-manifest.json');
            if (isManifest) {
                manifestUploadCount++;
            } else {
                csvUploadCount++;
                String payload = request.getBody();
                if (payload != null && payload.startsWith('Id,Name,AccountNumber,Type,Industry,Phone,Website')) {
                    sawCsvHeader = true;
                }
            }

            HttpResponse response = new HttpResponse();
            if (!isManifest && failFirstCsvUpload && csvFailureCount == 0) {
                csvFailureCount++;
                response.setStatusCode(503);
                response.setBody('temporary failure');
                return response;
            }

            response.setStatusCode(200);
            response.setBody('ok');
            return response;
        }
    }
}
