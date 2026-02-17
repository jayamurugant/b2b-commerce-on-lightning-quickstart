global with sharing class AccountS3ExportBatch
    implements Database.Batchable<Account>, Database.AllowsCallouts, Database.Stateful {

    private static final String CSV_HEADER =
        'Id,Name,AccountNumber,Type,Industry,Phone,Website,BillingCity,BillingState,BillingCountry,LastModifiedDateUtc';
    private static final Integer DEFAULT_BATCH_SIZE = 200;
    private static final Integer MAX_BATCH_SIZE = 2000;
    private static final String ISO_8601_UTC = 'yyyy-MM-dd\'T\'HH:mm:ss\'Z\'';

    private ExportConfig config;
    private String runToken;
    private Integer partCounter = 0;
    private Integer exportedRecordCount = 0;

    // clientId/clientSecret map to AWS Access Key ID and Secret Access Key.
    global AccountS3ExportBatch(
        String clientId,
        String clientSecret,
        String awsRegion,
        String bucketName,
        String objectPrefix
    ) {
        this(new ExportConfig(clientId, clientSecret, awsRegion, bucketName, objectPrefix, null));
    }

    global AccountS3ExportBatch(
        String clientId,
        String clientSecret,
        String awsRegion,
        String bucketName,
        String objectPrefix,
        String s3HostOverride
    ) {
        this(new ExportConfig(clientId, clientSecret, awsRegion, bucketName, objectPrefix, s3HostOverride));
    }

    global AccountS3ExportBatch(String metadataRecordDeveloperName) {
        this(ExportConfig.fromCustomMetadata(metadataRecordDeveloperName));
    }

    @TestVisible
    private AccountS3ExportBatch(ExportConfig exportConfig) {
        this.config = exportConfig;
        String timestamp = Datetime.now().formatGmt('yyyyMMdd\'T\'HHmmss\'Z\'');
        String randomToken = EncodingUtil.convertToHex(
            Crypto.generateDigest(
                'SHA-256',
                Blob.valueOf(String.valueOf(Crypto.getRandomLong()) + ':' + String.valueOf(Datetime.now().getTime()))
            )
        ).substring(0, 8).toLowerCase();
        this.runToken = timestamp + '-' + randomToken;
    }

    global Database.QueryLocator start(Database.BatchableContext batchContext) {
        return Database.getQueryLocator([
            SELECT
                Id,
                Name,
                AccountNumber,
                Type,
                Industry,
                Phone,
                Website,
                BillingCity,
                BillingState,
                BillingCountry,
                LastModifiedDate
            FROM Account
            ORDER BY Id
        ]);
    }

    global void execute(Database.BatchableContext batchContext, List<Account> scope) {
        if (scope == null || scope.isEmpty()) {
            return;
        }

        this.partCounter++;
        String objectKey = buildCsvObjectKey(this.partCounter);
        String csvPayload = buildCsvPayload(scope);

        new AwsS3Client(this.config).putObject(
            objectKey,
            Blob.valueOf(csvPayload),
            'text/csv; charset=utf-8'
        );

        this.exportedRecordCount += scope.size();
    }

    global void finish(Database.BatchableContext batchContext) {
        AwsS3Client s3Client = new AwsS3Client(this.config);
        if (this.partCounter == 0) {
            // Produce a deterministic empty export file even when no records exist.
            this.partCounter = 1;
            s3Client.putObject(
                buildCsvObjectKey(this.partCounter),
                Blob.valueOf(CSV_HEADER + '\n'),
                'text/csv; charset=utf-8'
            );
        }

        Map<String, Object> manifest = new Map<String, Object>{
            'jobId' => batchContext == null ? null : batchContext.getJobId(),
            'runToken' => this.runToken,
            'exportedAtUtc' => Datetime.now().formatGmt(ISO_8601_UTC),
            'bucketName' => this.config.bucketName,
            'region' => this.config.region,
            'objectPrefix' => this.config.objectPrefix,
            'recordCount' => this.exportedRecordCount,
            'partCount' => this.partCounter,
            'filePattern' => prefixWithObjectPath('accounts-export-' + this.runToken + '-part-######.csv')
        };

        s3Client.putObject(
            buildManifestObjectKey(),
            Blob.valueOf(JSON.serializePretty(manifest)),
            'application/json'
        );
    }

    global static Id runNow(
        String clientId,
        String clientSecret,
        String awsRegion,
        String bucketName,
        String objectPrefix,
        Integer batchSize
    ) {
        return Database.executeBatch(
            new AccountS3ExportBatch(clientId, clientSecret, awsRegion, bucketName, objectPrefix),
            normalizeBatchSize(batchSize)
        );
    }

    global static Id runNow(String metadataRecordDeveloperName, Integer batchSize) {
        return Database.executeBatch(
            new AccountS3ExportBatch(metadataRecordDeveloperName),
            normalizeBatchSize(batchSize)
        );
    }

    private static Integer normalizeBatchSize(Integer batchSize) {
        if (batchSize == null || batchSize <= 0 || batchSize > MAX_BATCH_SIZE) {
            return DEFAULT_BATCH_SIZE;
        }
        return batchSize;
    }

    private String buildCsvPayload(List<Account> accounts) {
        List<String> lines = new List<String>{CSV_HEADER};
        for (Account accountRecord : accounts) {
            lines.add(toCsvRow(accountRecord));
        }
        return String.join(lines, '\n') + '\n';
    }

    private String toCsvRow(Account accountRecord) {
        return String.join(
            new List<String>{
                escapeCsv(accountRecord.Id),
                escapeCsv(accountRecord.Name),
                escapeCsv(accountRecord.AccountNumber),
                escapeCsv(accountRecord.Type),
                escapeCsv(accountRecord.Industry),
                escapeCsv(accountRecord.Phone),
                escapeCsv(accountRecord.Website),
                escapeCsv(accountRecord.BillingCity),
                escapeCsv(accountRecord.BillingState),
                escapeCsv(accountRecord.BillingCountry),
                escapeCsv(
                    accountRecord.LastModifiedDate == null
                        ? null
                        : accountRecord.LastModifiedDate.formatGmt(ISO_8601_UTC)
                )
            },
            ','
        );
    }

    private static String escapeCsv(Object rawValue) {
        String value = rawValue == null ? '' : String.valueOf(rawValue);
        return '"' + value.replace('"', '""') + '"';
    }

    private String buildCsvObjectKey(Integer partNumber) {
        String fileName =
            'accounts-export-' + this.runToken + '-part-' + leftPad(partNumber, 6) + '.csv';
        return prefixWithObjectPath(fileName);
    }

    private String buildManifestObjectKey() {
        return prefixWithObjectPath('accounts-export-' + this.runToken + '-manifest.json');
    }

    private String prefixWithObjectPath(String fileName) {
        if (String.isBlank(this.config.objectPrefix)) {
            return fileName;
        }
        return trimSlashes(this.config.objectPrefix) + '/' + fileName;
    }

    private static String leftPad(Integer value, Integer totalLength) {
        String result = String.valueOf(value);
        while (result.length() < totalLength) {
            result = '0' + result;
        }
        return result;
    }

    private static String trimSlashes(String inputValue) {
        String result = inputValue == null ? '' : inputValue.trim();
        while (result.startsWith('/')) {
            result = result.substring(1);
        }
        while (result.endsWith('/')) {
            result = result.substring(0, result.length() - 1);
        }
        return result;
    }

    public class AccountS3ExportException extends Exception {}

    private class ExportConfig {
        public String clientId;
        public String clientSecret;
        public String region;
        public String bucketName;
        public String objectPrefix;
        public String s3Host;

        private ExportConfig(
            String clientId,
            String clientSecret,
            String awsRegion,
            String bucketName,
            String objectPrefix,
            String s3HostOverride
        ) {
            this.clientId = requireValue(clientId, 'clientId');
            this.clientSecret = requireValue(clientSecret, 'clientSecret');
            this.region = requireValue(awsRegion, 'awsRegion').toLowerCase();
            this.bucketName = requireValue(bucketName, 'bucketName');
            this.objectPrefix = trimSlashes(objectPrefix);
            this.s3Host = normalizeHost(s3HostOverride, this.region);
        }

        public static ExportConfig fromCustomMetadata(String metadataRecordDeveloperName) {
            String recordName = requireValue(
                metadataRecordDeveloperName,
                'metadataRecordDeveloperName'
            );

            String typeName = 'S3_Export_Settings__mdt';
            if (!Schema.getGlobalDescribe().containsKey(typeName)) {
                throw new AccountS3ExportException(
                    'Missing custom metadata type ' + typeName + '.'
                    + ' Create it with fields ClientId__c, ClientSecret__c, AwsRegion__c, BucketName__c, '
                    + 'ObjectPrefix__c, and optional S3Host__c.'
                );
            }

            List<SObject> settings = Database.query(
                'SELECT ClientId__c, ClientSecret__c, AwsRegion__c, BucketName__c, ObjectPrefix__c, S3Host__c '
                + 'FROM ' + typeName + ' WHERE DeveloperName = :recordName LIMIT 1'
            );
            if (settings.isEmpty()) {
                throw new AccountS3ExportException(
                    'No S3 export settings found for custom metadata record: ' + recordName
                );
            }

            SObject setting = settings[0];
            return new ExportConfig(
                (String) setting.get('ClientId__c'),
                (String) setting.get('ClientSecret__c'),
                (String) setting.get('AwsRegion__c'),
                (String) setting.get('BucketName__c'),
                (String) setting.get('ObjectPrefix__c'),
                (String) setting.get('S3Host__c')
            );
        }

        private static String requireValue(String inputValue, String fieldName) {
            if (String.isBlank(inputValue)) {
                throw new AccountS3ExportException('Missing required value: ' + fieldName);
            }
            return inputValue.trim();
        }

        private static String normalizeHost(String s3HostOverride, String region) {
            String host =
                String.isBlank(s3HostOverride)
                    ? 's3.' + region + '.amazonaws.com'
                    : s3HostOverride.trim().toLowerCase();
            if (host.startsWith('https://')) {
                host = host.substring(8);
            } else if (host.startsWith('http://')) {
                host = host.substring(7);
            }
            if (host.endsWith('/')) {
                host = host.substring(0, host.length() - 1);
            }
            if (String.isBlank(host)) {
                throw new AccountS3ExportException('Invalid s3Host value.');
            }
            return host;
        }
    }

    private class AwsS3Client {
        private static final Integer MAX_ATTEMPTS = 3;
        private ExportConfig config;

        private AwsS3Client(ExportConfig exportConfig) {
            this.config = exportConfig;
        }

        public void putObject(String objectKey, Blob payload, String contentType) {
            Integer attempt = 0;
            while (attempt < MAX_ATTEMPTS) {
                attempt++;
                HttpResponse response;
                try {
                    response = new Http().send(buildSignedPutRequest(objectKey, payload, contentType));
                } catch (System.CalloutException ex) {
                    if (attempt >= MAX_ATTEMPTS) {
                        throw new AccountS3ExportException(
                            'Network failure uploading [' + objectKey + '] after '
                            + attempt + ' attempts: ' + ex.getMessage()
                        );
                    }
                    continue;
                }

                Integer statusCode = response.getStatusCode();
                if (statusCode >= 200 && statusCode < 300) {
                    return;
                }
                if (!isRetryableStatus(statusCode) || attempt >= MAX_ATTEMPTS) {
                    throw new AccountS3ExportException(
                        'S3 rejected upload for [' + objectKey + '] with status '
                        + statusCode + '. Response body: ' + response.getBody()
                    );
                }
            }
        }

        private HttpRequest buildSignedPutRequest(String objectKey, Blob payload, String contentType) {
            String normalizedObjectKey = trimSlashes(objectKey);
            if (String.isBlank(normalizedObjectKey)) {
                throw new AccountS3ExportException('Object key cannot be blank.');
            }

            Datetime requestTime = Datetime.now();
            String amzDate = requestTime.formatGmt('yyyyMMdd\'T\'HHmmss\'Z\'');
            String dateStamp = requestTime.formatGmt('yyyyMMdd');
            String objectPath = canonicalizePath(normalizedObjectKey);

            Boolean usePathStyleEndpoint = this.config.bucketName.contains('.');
            String host = usePathStyleEndpoint
                ? this.config.s3Host
                : this.config.bucketName + '.' + this.config.s3Host;
            String canonicalUri = usePathStyleEndpoint
                ? canonicalizePath(this.config.bucketName) + objectPath
                : objectPath;
            String payloadHash = sha256Hex(payload);
            String signedHeaders = 'host;x-amz-content-sha256;x-amz-date';

            String canonicalHeaders =
                'host:' + host + '\n'
                + 'x-amz-content-sha256:' + payloadHash + '\n'
                + 'x-amz-date:' + amzDate + '\n';
            String canonicalRequest =
                'PUT\n'
                + canonicalUri + '\n'
                + '\n'
                + canonicalHeaders + '\n'
                + signedHeaders + '\n'
                + payloadHash;

            String credentialScope = dateStamp + '/' + this.config.region + '/s3/aws4_request';
            String stringToSign =
                'AWS4-HMAC-SHA256\n'
                + amzDate + '\n'
                + credentialScope + '\n'
                + sha256Hex(Blob.valueOf(canonicalRequest));

            String signature = calculateSignature(
                this.config.clientSecret,
                dateStamp,
                this.config.region,
                stringToSign
            );
            String authorizationHeader =
                'AWS4-HMAC-SHA256 Credential=' + this.config.clientId + '/' + credentialScope
                + ', SignedHeaders=' + signedHeaders
                + ', Signature=' + signature;

            HttpRequest request = new HttpRequest();
            request.setEndpoint('https://' + host + canonicalUri);
            request.setMethod('PUT');
            request.setHeader('Host', host);
            request.setHeader('x-amz-content-sha256', payloadHash);
            request.setHeader('x-amz-date', amzDate);
            request.setHeader('Authorization', authorizationHeader);
            request.setHeader('Content-Type', contentType);
            request.setBodyAsBlob(payload);
            request.setTimeout(120000);
            return request;
        }

        private static Boolean isRetryableStatus(Integer statusCode) {
            return statusCode == 429 || statusCode == 500 || statusCode == 502 || statusCode == 503 || statusCode == 504;
        }

        private static String canonicalizePath(String pathValue) {
            String normalized = trimSlashes(pathValue);
            if (String.isBlank(normalized)) {
                throw new AccountS3ExportException('Path cannot be blank.');
            }
            List<String> encodedSegments = new List<String>();
            for (String segment : normalized.split('/')) {
                encodedSegments.add(urlEncodePathSegment(segment));
            }
            return '/' + String.join(encodedSegments, '/');
        }

        private static String urlEncodePathSegment(String value) {
            String encoded = EncodingUtil.urlEncode(value, 'UTF-8');
            encoded = encoded.replace('+', '%20');
            encoded = encoded.replace('%7E', '~');
            return encoded;
        }

        private static String sha256Hex(Blob inputValue) {
            return EncodingUtil.convertToHex(
                Crypto.generateDigest('SHA-256', inputValue)
            ).toLowerCase();
        }

        private static String calculateSignature(
            String secretKey,
            String dateStamp,
            String region,
            String stringToSign
        ) {
            Blob kDate = Crypto.generateMac(
                'HmacSHA256',
                Blob.valueOf(dateStamp),
                Blob.valueOf('AWS4' + secretKey)
            );
            Blob kRegion = Crypto.generateMac('HmacSHA256', Blob.valueOf(region), kDate);
            Blob kService = Crypto.generateMac('HmacSHA256', Blob.valueOf('s3'), kRegion);
            Blob kSigning = Crypto.generateMac('HmacSHA256', Blob.valueOf('aws4_request'), kService);
            Blob signatureBlob = Crypto.generateMac(
                'HmacSHA256',
                Blob.valueOf(stringToSign),
                kSigning
            );
            return EncodingUtil.convertToHex(signatureBlob).toLowerCase();
        }
    }
}
