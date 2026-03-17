/**
 * @description       : Inbound REST API connector exposing Salesforce data
 *                      to external systems (e.g., the Python migration pipeline).
 *                      Decorated with @RestResource — accessible at:
 *                      /services/apexrest/migration/v1/
 *
 *                      Endpoints:
 *                        GET    /accounts           — paginated account list
 *                        GET    /accounts/{legacyId} — single account by legacy ID
 *                        POST   /accounts/upsert    — bulk upsert from migration pipeline
 *                        GET    /status             — migration health check
 *                        POST   /batches            — launch a batch job
 *                        DELETE /batches/{jobId}    — abort a running batch
 *
 * @author            : Migration Platform Team
 * @group             : Integration
 * @last modified on  : 2026-03-16
 **/
@RestResource(urlMapping='/migration/v1/*')
global with sharing class RestApiConnector {

    private static final String  LOG_SOURCE      = 'RestApiConnector';
    private static final Integer DEFAULT_PAGE_SIZE = 200;
    private static final Integer MAX_PAGE_SIZE     = 2000;

    // ─── Custom Exception ────────────────────────────────────────────────────
    global class RestApiException extends Exception {}

    // =========================================================================
    // HTTP GET
    // =========================================================================

    /**
     * @description Routes incoming GET requests.
     *              Supported paths: /accounts, /accounts/{legacyId}, /status
     */
    @HttpGet
    global static void doGet() {
        RestRequest  req  = RestContext.request;
        RestResponse resp = RestContext.response;

        try {
            String path = normalizePath(req.requestURI);

            if (path.startsWith('/accounts/')) {
                String legacyId = path.substringAfter('/accounts/');
                handleGetAccountByLegacyId(legacyId, resp);

            } else if (path == '/accounts' || path == '/accounts/') {
                handleGetAccounts(req, resp);

            } else if (path == '/status') {
                handleGetStatus(resp);

            } else {
                sendError(resp, 404, 'NOT_FOUND', 'Path not found: ' + path);
            }
        } catch (Exception e) {
            AuditLogService.logError(LOG_SOURCE, 'doGet', e.getMessage(), null);
            sendError(resp, 500, 'INTERNAL_ERROR', e.getMessage());
        }
    }

    // =========================================================================
    // HTTP POST
    // =========================================================================

    /**
     * @description Routes incoming POST requests.
     *              Supported paths: /accounts/upsert, /batches
     */
    @HttpPost
    global static void doPost() {
        RestRequest  req  = RestContext.request;
        RestResponse resp = RestContext.response;

        try {
            String path = normalizePath(req.requestURI);
            String body = req.requestBody != null ? req.requestBody.toString() : '{}';

            if (path == '/accounts/upsert') {
                handleUpsertAccounts(body, resp);

            } else if (path == '/batches') {
                handleLaunchBatch(body, resp);

            } else {
                sendError(resp, 404, 'NOT_FOUND', 'Path not found: ' + path);
            }
        } catch (Exception e) {
            AuditLogService.logError(LOG_SOURCE, 'doPost', e.getMessage(), null);
            sendError(resp, 500, 'INTERNAL_ERROR', e.getMessage());
        }
    }

    // =========================================================================
    // HTTP DELETE
    // =========================================================================

    /**
     * @description Aborts a running batch job.
     *              Path: /batches/{jobId}
     */
    @HttpDelete
    global static void doDelete() {
        RestRequest  req  = RestContext.request;
        RestResponse resp = RestContext.response;

        try {
            String path = normalizePath(req.requestURI);
            if (path.startsWith('/batches/')) {
                String jobId = path.substringAfter('/batches/');
                handleAbortBatch(jobId, resp);
            } else {
                sendError(resp, 404, 'NOT_FOUND', 'Path not found: ' + path);
            }
        } catch (Exception e) {
            AuditLogService.logError(LOG_SOURCE, 'doDelete', e.getMessage(), null);
            sendError(resp, 500, 'INTERNAL_ERROR', e.getMessage());
        }
    }

    // =========================================================================
    // ROUTE HANDLERS
    // =========================================================================

    private static void handleGetAccounts(RestRequest req, RestResponse resp) {
        Integer pageNum  = getIntParam(req, 'page',     0);
        Integer pageSize = getIntParam(req, 'pageSize', DEFAULT_PAGE_SIZE);
        String  status   = req.params.get('migrationStatus');

        if (pageSize > MAX_PAGE_SIZE) { pageSize = MAX_PAGE_SIZE; }

        Integer offset = pageNum * pageSize;
        String  soql   = 'SELECT Id, Name, Legacy_ID__c, Migration_Status__c, '
                       + 'Type, Industry, Phone, Website, AnnualRevenue, '
                       + 'BillingStreet, BillingCity, BillingState, BillingPostalCode, '
                       + 'BillingCountry, CreatedDate, LastModifiedDate '
                       + 'FROM Account ';
        if (String.isNotBlank(status)) {
            soql += 'WHERE Migration_Status__c = \'' + String.escapeSingleQuotes(status) + '\' ';
        }
        soql += 'ORDER BY CreatedDate ASC LIMIT ' + pageSize + ' OFFSET ' + offset;

        List<Account> accounts = Database.query(soql);
        Integer total = [SELECT COUNT() FROM Account];

        Map<String, Object> payload = new Map<String, Object>{
            'records'    => accounts,
            'pageNumber' => pageNum,
            'pageSize'   => pageSize,
            'totalCount' => total,
            'hasMore'    => (offset + pageSize) < total
        };
        sendSuccess(resp, 200, payload);

        AuditLogService.logSuccess(LOG_SOURCE, 'GET /accounts',
            'Returned ' + accounts.size() + ' accounts. Page=' + pageNum, null);
    }

    private static void handleGetAccountByLegacyId(String legacyId, RestResponse resp) {
        if (String.isBlank(legacyId)) {
            sendError(resp, 400, 'BAD_REQUEST', 'Legacy ID is required.');
            return;
        }
        List<Account> results = [
            SELECT Id, Name, Legacy_ID__c, Migration_Status__c, Type, Industry,
                   Phone, Website, AnnualRevenue, BillingStreet, BillingCity,
                   BillingState, BillingPostalCode, BillingCountry,
                   CreatedDate, LastModifiedDate, Active__c
            FROM   Account
            WHERE  Legacy_ID__c = :legacyId
            LIMIT  1
        ];
        if (results.isEmpty()) {
            sendError(resp, 404, 'NOT_FOUND', 'Account not found for legacyId: ' + legacyId);
            return;
        }
        sendSuccess(resp, 200, results[0]);
    }

    private static void handleGetStatus(RestResponse resp) {
        Map<String, Object> counts = new Map<String, Object>();
        for (AggregateResult ar : [
            SELECT Migration_Status__c status, COUNT(Id) cnt
            FROM   Account
            GROUP BY Migration_Status__c
        ]) {
            counts.put((String)ar.get('status'), ar.get('cnt'));
        }
        Integer batchesRunning = [
            SELECT COUNT() FROM AsyncApexJob
            WHERE  ApexClass.Name = 'MigrationBatchProcessor'
            AND    Status IN ('Queued', 'Processing', 'Preparing')
        ];
        Map<String, Object> payload = new Map<String, Object>{
            'status'          => 'OK',
            'timestamp'       => System.now().format(),
            'orgId'           => UserInfo.getOrganizationId(),
            'migrationCounts' => counts,
            'activeBatches'   => batchesRunning,
            'apiVersion'      => 'v1'
        };
        sendSuccess(resp, 200, payload);
    }

    private static void handleUpsertAccounts(String body, RestResponse resp) {
        Map<String, Object> parsed;
        try {
            parsed = (Map<String, Object>) JSON.deserializeUntyped(body);
        } catch (JSONException je) {
            sendError(resp, 400, 'INVALID_JSON', 'Request body is not valid JSON.');
            return;
        }

        List<Object> rawRecords = (List<Object>) parsed.get('records');
        if (rawRecords == null || rawRecords.isEmpty()) {
            sendError(resp, 400, 'BAD_REQUEST', '"records" array is required and non-empty.');
            return;
        }

        List<Account> accounts = new List<Account>();
        for (Object raw : rawRecords) {
            Map<String, Object> rec = (Map<String, Object>) raw;
            Account acc = new Account();
            acc.Legacy_ID__c        = (String) rec.get('legacyId');
            acc.Name                = (String) rec.get('name');
            acc.Type                = (String) rec.get('type');
            acc.Industry            = (String) rec.get('industry');
            acc.Phone               = (String) rec.get('phone');
            acc.Website             = (String) rec.get('website');
            acc.BillingStreet       = (String) rec.get('billingStreet');
            acc.BillingCity         = (String) rec.get('billingCity');
            acc.BillingState        = (String) rec.get('billingState');
            acc.BillingPostalCode   = (String) rec.get('billingPostalCode');
            acc.BillingCountry      = (String) rec.get('billingCountry');
            acc.Migration_Status__c = (String) rec.get('migrationStatus');
            if (rec.get('annualRevenue') != null) {
                acc.AnnualRevenue = Decimal.valueOf(String.valueOf(rec.get('annualRevenue')));
            }
            accounts.add(acc);
        }

        AccountService.BulkUpsertResult result = AccountService.bulkUpsertAccounts(accounts);

        Map<String, Object> payload = new Map<String, Object>{
            'successCount'  => result.successCount,
            'failureCount'  => result.failureCount,
            'insertedCount' => result.insertedCount,
            'updatedCount'  => result.updatedCount,
            'successRate'   => result.successRate(),
            'failures'      => result.failures
        };
        sendSuccess(resp, result.failureCount == 0 ? 200 : 207, payload);

        AuditLogService.logSuccess(LOG_SOURCE, 'POST /accounts/upsert',
            'Upserted ' + accounts.size() + ' accounts. '
            + 'Success=' + result.successCount + ', Failed=' + result.failureCount, null);
    }

    private static void handleLaunchBatch(String body, RestResponse resp) {
        Map<String, Object> config;
        try {
            config = (Map<String, Object>) JSON.deserializeUntyped(body);
        } catch (JSONException je) {
            sendError(resp, 400, 'INVALID_JSON', 'Request body is not valid JSON.');
            return;
        }

        String  objectName = (String)  config.get('objectName');
        String  status     = (String)  config.get('migrationStatus');
        Boolean dryRun     = config.get('dryRun') != null
            ? (Boolean) config.get('dryRun') : false;
        Integer maxErrors  = config.get('maxErrors') != null
            ? Integer.valueOf(String.valueOf(config.get('maxErrors'))) : 500;
        Integer batchSize  = config.get('batchSize') != null
            ? Integer.valueOf(String.valueOf(config.get('batchSize'))) : 200;

        if (String.isBlank(objectName)) { objectName = 'Account'; }
        if (String.isBlank(status))     { status     = 'Pending'; }

        MigrationBatchProcessor processor = new MigrationBatchProcessor(
            objectName, status, dryRun, maxErrors);
        Id jobId = Database.executeBatch(processor, batchSize);

        Map<String, Object> payload = new Map<String, Object>{
            'jobId'      => jobId,
            'objectName' => objectName,
            'status'     => 'Queued',
            'dryRun'     => dryRun,
            'batchSize'  => batchSize
        };
        sendSuccess(resp, 202, payload);

        AuditLogService.logSuccess(LOG_SOURCE, 'POST /batches',
            'Batch launched. JobId=' + jobId + ', Object=' + objectName
            + ', DryRun=' + dryRun, null);
    }

    private static void handleAbortBatch(String jobId, RestResponse resp) {
        if (String.isBlank(jobId)) {
            sendError(resp, 400, 'BAD_REQUEST', 'jobId is required.');
            return;
        }
        try {
            System.abortJob(jobId);
            sendSuccess(resp, 200, new Map<String, Object>{
                'jobId'   => jobId,
                'status'  => 'Aborted',
                'message' => 'Batch job aborted successfully.'
            });
            AuditLogService.logSuccess(LOG_SOURCE, 'DELETE /batches',
                'Batch aborted. JobId=' + jobId, null);
        } catch (Exception e) {
            sendError(resp, 400, 'ABORT_FAILED',
                'Could not abort job ' + jobId + ': ' + e.getMessage());
        }
    }

    // =========================================================================
    // RESPONSE HELPERS
    // =========================================================================

    private static void sendSuccess(RestResponse resp, Integer statusCode, Object payload) {
        resp.statusCode = statusCode;
        resp.addHeader('Content-Type', 'application/json');
        resp.responseBody = Blob.valueOf(JSON.serialize(new Map<String, Object>{
            'success'   => true,
            'timestamp' => System.now().format(),
            'data'      => payload
        }));
    }

    private static void sendError(RestResponse resp, Integer statusCode,
                                   String errorCode, String message) {
        resp.statusCode = statusCode;
        resp.addHeader('Content-Type', 'application/json');
        resp.responseBody = Blob.valueOf(JSON.serialize(new Map<String, Object>{
            'success'   => false,
            'timestamp' => System.now().format(),
            'error'     => new Map<String, Object>{
                'code'    => errorCode,
                'message' => message
            }
        }));
        AuditLogService.logError(LOG_SOURCE, 'sendError',
            '[' + statusCode + '] ' + errorCode + ': ' + message, null);
    }

    private static String normalizePath(String uri) {
        if (uri == null) { return '/'; }
        // Strip the base path prefix e.g. /services/apexrest/migration/v1
        String base = '/migration/v1';
        Integer idx = uri.indexOf(base);
        if (idx >= 0) {
            String path = uri.substring(idx + base.length());
            return String.isBlank(path) ? '/' : path;
        }
        return uri;
    }

    private static Integer getIntParam(RestRequest req, String key, Integer defaultVal) {
        String val = req.params.get(key);
        if (String.isBlank(val)) { return defaultVal; }
        try { return Integer.valueOf(val); }
        catch (Exception e) { return defaultVal; }
    }
}
