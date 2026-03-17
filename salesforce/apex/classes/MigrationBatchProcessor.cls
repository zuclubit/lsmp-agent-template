/**
 * @description       : Batch processor for migrating legacy records into Salesforce.
 *                      Implements Database.Batchable and Database.Stateful for
 *                      cross-batch state aggregation and full error tracking.
 * @author            : Migration Platform Team
 * @group             : Migration
 * @last modified on  : 2026-03-16
 **/
public with sharing class MigrationBatchProcessor
    implements Database.Batchable<SObject>, Database.Stateful, Database.AllowsCallouts {

    // ─── Custom Exception ────────────────────────────────────────────────────
    public class MigrationBatchException extends Exception {}

    // ─── Stateful counters (survive across batch chunks) ─────────────────────
    @TestVisible private Integer totalProcessed = 0;
    @TestVisible private Integer totalSuccess   = 0;
    @TestVisible private Integer totalFailed    = 0;
    @TestVisible private String  batchJobId;
    @TestVisible private String  batchRunId;
    @TestVisible private List<String> errorLog  = new List<String>();

    // ─── Configuration ───────────────────────────────────────────────────────
    private final String  migrationBatchLabel;
    private final String  objectApiName;
    private final String  migrationStatus;
    private final Boolean dryRun;
    private final Integer maxErrorThreshold;

    private static final String  LOG_SOURCE         = 'MigrationBatchProcessor';
    private static final Integer DEFAULT_MAX_ERRORS = 500;

    // =========================================================================
    // CONSTRUCTORS
    // =========================================================================

    /**
     * @description Default constructor — processes all Accounts in 'Pending' status.
     */
    public MigrationBatchProcessor() {
        this('Account', 'Pending', false, DEFAULT_MAX_ERRORS);
    }

    /**
     * @description Full constructor with configuration options.
     * @param objectApiName      API name of the object to migrate
     * @param migrationStatus    Migration_Status__c value to query
     * @param dryRun             If true, transform only — no DML
     * @param maxErrorThreshold  Abort batch if failures exceed this count
     */
    public MigrationBatchProcessor(String objectApiName, String migrationStatus,
                                    Boolean dryRun, Integer maxErrorThreshold) {
        this.objectApiName       = objectApiName;
        this.migrationStatus     = migrationStatus;
        this.dryRun              = dryRun;
        this.maxErrorThreshold   = maxErrorThreshold != null ? maxErrorThreshold : DEFAULT_MAX_ERRORS;
        this.migrationBatchLabel = objectApiName + '_' + DateTime.now().format('yyyyMMdd_HHmmss');
        this.batchRunId          = generateRunId();
    }

    // =========================================================================
    // DATABASE.BATCHABLE — START
    // =========================================================================

    /**
     * @description Defines the scope of records to process.
     *              Queries records flagged for migration with 'Pending' status.
     * @param bc    BatchableContext provided by the platform
     * @return      QueryLocator for the batch scope
     */
    public Database.QueryLocator start(Database.BatchableContext bc) {
        this.batchJobId = bc.getJobId();

        AuditLogService.logSuccess(LOG_SOURCE, 'BatchStart',
            'Batch started. JobId=' + this.batchJobId
            + ', RunId=' + this.batchRunId
            + ', Object=' + this.objectApiName
            + ', DryRun=' + this.dryRun, null);

        return Database.getQueryLocator(
            'SELECT Id, Name, Legacy_ID__c, Migration_Status__c, Migration_Batch__c, '
          + '       Phone, Industry, Type, AnnualRevenue, '
          + '       BillingStreet, BillingCity, BillingState, BillingPostalCode, BillingCountry, '
          + '       Website, Description, OwnerId '
          + 'FROM   Account '
          + 'WHERE  Migration_Status__c = :migrationStatus '
          + 'ORDER BY CreatedDate ASC'
        );
    }

    // =========================================================================
    // DATABASE.BATCHABLE — EXECUTE
    // =========================================================================

    /**
     * @description Processes each batch chunk: transforms and upserts records.
     * @param bc     BatchableContext
     * @param scope  Current chunk of Account records
     */
    public void execute(Database.BatchableContext bc, List<SObject> scope) {
        // Abort if error threshold exceeded
        if (totalFailed >= maxErrorThreshold) {
            AuditLogService.logError(LOG_SOURCE, 'BatchExecute',
                'Error threshold exceeded (' + maxErrorThreshold + '). Aborting.', null);
            System.abortJob(bc.getJobId());
            return;
        }

        List<Account> accounts = (List<Account>) scope;
        List<Account> toUpsert = new List<Account>();

        for (Account acc : accounts) {
            try {
                Account transformed = transformRecord(acc);
                transformed.Migration_Batch__c = this.migrationBatchLabel;
                toUpsert.add(transformed);
            } catch (Exception e) {
                totalFailed++;
                logError(acc.Id, acc.Legacy_ID__c,
                    'Transform failed: ' + e.getMessage());
            }
        }

        totalProcessed += accounts.size();

        if (dryRun || toUpsert.isEmpty()) {
            totalSuccess += toUpsert.size();
            return;
        }

        // Perform DML
        List<Database.UpsertResult> results =
            Database.upsert(toUpsert, Account.Legacy_ID__c, false);

        for (Integer i = 0; i < results.size(); i++) {
            Database.UpsertResult ur = results[i];
            if (ur.isSuccess()) {
                totalSuccess++;
                markRecordSuccess(toUpsert[i].Id != null ? toUpsert[i].Id : ur.getId());
            } else {
                totalFailed++;
                String errMsg = buildErrorMessage(ur.getErrors());
                logError(toUpsert[i].Id, toUpsert[i].Legacy_ID__c, 'Upsert failed: ' + errMsg);
                markRecordFailed(toUpsert[i].Id, errMsg);
            }
        }
    }

    // =========================================================================
    // DATABASE.BATCHABLE — FINISH
    // =========================================================================

    /**
     * @description Finalizes the batch: writes summary, sends notification.
     * @param bc  BatchableContext
     */
    public void finish(Database.BatchableContext bc) {
        String summary = buildSummary();
        AuditLogService.logSuccess(LOG_SOURCE, 'BatchFinish', summary, null);

        // Write final migration log entry
        writeMigrationLog(summary);

        // Send completion email to admin (if not dry-run)
        if (!dryRun) {
            sendCompletionNotification(summary);
        }
    }

    // =========================================================================
    // TRANSFORMATION LOGIC
    // =========================================================================

    /**
     * @description Transforms a raw legacy Account into a clean Salesforce Account.
     * @param  source  Source Account from query
     * @return         Transformed Account ready for upsert
     */
    @TestVisible
    private Account transformRecord(Account source) {
        Account target = new Account();

        // Core identification
        target.Id         = source.Id;
        target.Legacy_ID__c = source.Legacy_ID__c;
        target.Name       = cleanString(source.Name, 255);

        // Classify account type
        target.Type       = resolveAccountType(source.Type);

        // Normalize phone
        target.Phone      = normalizePhone(source.Phone);

        // Industry mapping
        target.Industry   = mapIndustry(source.Industry);

        // Financial
        target.AnnualRevenue = source.AnnualRevenue != null
            ? source.AnnualRevenue.setScale(2) : null;

        // Address
        target.BillingStreet     = cleanString(source.BillingStreet, 255);
        target.BillingCity       = cleanString(source.BillingCity, 40);
        target.BillingState      = cleanString(source.BillingState, 80);
        target.BillingPostalCode = cleanString(source.BillingPostalCode, 20);
        target.BillingCountry    = resolveCountryCode(source.BillingCountry);

        // Web / description
        target.Website            = validateUrl(source.Website);
        target.Description        = cleanString(source.Description, 32000);

        // Status
        target.Migration_Status__c = 'In Progress';
        target.OwnerId             = source.OwnerId;

        return target;
    }

    // ─── Field-level transformation helpers ──────────────────────────────────

    private String cleanString(String val, Integer maxLen) {
        if (String.isBlank(val)) { return null; }
        String cleaned = val.trim().replaceAll('[\\r\\n\\t]', ' ');
        return cleaned.length() > maxLen ? cleaned.substring(0, maxLen) : cleaned;
    }

    private String normalizePhone(String raw) {
        if (String.isBlank(raw)) { return null; }
        String digits = raw.replaceAll('[^0-9+]', '');
        if (digits.length() == 10) {
            return '(' + digits.substring(0,3) + ') '
                + digits.substring(3,6) + '-' + digits.substring(6);
        }
        return raw.length() <= 40 ? raw : raw.substring(0, 40);
    }

    private String resolveAccountType(String raw) {
        if (String.isBlank(raw)) { return 'Prospect'; }
        Map<String, String> typeMap = new Map<String, String>{
            'CUSTOMER'   => 'Customer - Direct',
            'PARTNER'    => 'Channel Partner / Reseller',
            'PROSPECT'   => 'Prospect',
            'VENDOR'     => 'Technology Partner',
            'INTERNAL'   => 'Other',
            'COMPETITOR' => 'Competitor'
        };
        return typeMap.containsKey(raw.toUpperCase())
            ? typeMap.get(raw.toUpperCase()) : 'Prospect';
    }

    private String mapIndustry(String raw) {
        if (String.isBlank(raw)) { return null; }
        Map<String, String> industryMap = new Map<String, String>{
            'TECH'         => 'Technology',
            'FINANCE'      => 'Finance',
            'BANKING'      => 'Finance',
            'HEALTHCARE'   => 'Healthcare',
            'RETAIL'       => 'Retail',
            'MANUFACTURING'=> 'Manufacturing',
            'GOVERNMENT'   => 'Government',
            'EDUCATION'    => 'Education',
            'ENERGY'       => 'Energy',
            'INSURANCE'    => 'Insurance'
        };
        String key = raw.toUpperCase().replaceAll('[^A-Z]','');
        return industryMap.containsKey(key) ? industryMap.get(key) : raw;
    }

    private String resolveCountryCode(String raw) {
        if (String.isBlank(raw)) { return 'US'; }
        if (raw.length() == 2)   { return raw.toUpperCase(); }
        Map<String, String> countryMap = new Map<String, String>{
            'UNITED STATES' => 'US', 'USA' => 'US', 'CANADA' => 'CA',
            'UNITED KINGDOM'=> 'GB', 'UK'  => 'GB', 'GERMANY'=> 'DE',
            'FRANCE'        => 'FR', 'SPAIN'=> 'ES', 'ITALY' => 'IT'
        };
        return countryMap.containsKey(raw.toUpperCase()) ?
            countryMap.get(raw.toUpperCase()) : raw.substring(0, Math.min(raw.length(), 2)).toUpperCase();
    }

    private String validateUrl(String raw) {
        if (String.isBlank(raw)) { return null; }
        String url = raw.trim();
        if (!url.startsWith('http://') && !url.startsWith('https://')) {
            url = 'https://' + url;
        }
        return url.length() <= 255 ? url : null;
    }

    // ─── DML status helpers ───────────────────────────────────────────────────

    private void markRecordSuccess(Id recordId) {
        if (recordId == null) { return; }
        // Fire-and-forget update via future to avoid DML recursion in batch execute
        updateMigrationStatusAsync(new List<Id>{recordId}, 'Migrated');
    }

    private void markRecordFailed(Id recordId, String errorMsg) {
        if (recordId == null) { return; }
        updateMigrationStatusAsync(new List<Id>{recordId}, 'Failed');
    }

    @future
    private static void updateMigrationStatusAsync(List<Id> ids, String status) {
        List<Account> toUpdate = new List<Account>();
        for (Id rid : ids) {
            toUpdate.add(new Account(Id = rid, Migration_Status__c = status));
        }
        if (!toUpdate.isEmpty()) {
            Database.update(toUpdate, false);
        }
    }

    // ─── Error log ────────────────────────────────────────────────────────────

    private void logError(Id recordId, String legacyId, String message) {
        String entry = DateTime.now().format() + ' | RecordId=' + recordId
            + ' | LegacyId=' + legacyId + ' | ' + message;
        errorLog.add(entry);
        AuditLogService.logError(LOG_SOURCE, 'BatchExecute', entry, recordId);
    }

    private String buildErrorMessage(List<Database.Error> errors) {
        List<String> msgs = new List<String>();
        for (Database.Error e : errors) {
            msgs.add(e.getStatusCode() + ': ' + e.getMessage());
        }
        return String.join(msgs, ' | ');
    }

    // ─── Summary ─────────────────────────────────────────────────────────────

    private String buildSummary() {
        return 'Migration Batch Complete'
            + ' | RunId='      + batchRunId
            + ' | JobId='      + batchJobId
            + ' | Object='     + objectApiName
            + ' | Processed='  + totalProcessed
            + ' | Success='    + totalSuccess
            + ' | Failed='     + totalFailed
            + ' | DryRun='     + dryRun
            + ' | Errors='     + errorLog.size();
    }

    private void writeMigrationLog(String summary) {
        try {
            MigrationLog__c log = new MigrationLog__c(
                Name           = migrationBatchLabel,
                Batch_Job_Id__c= batchJobId,
                Run_Id__c      = batchRunId,
                Object_Name__c = objectApiName,
                Total_Processed__c = totalProcessed,
                Total_Success__c   = totalSuccess,
                Total_Failed__c    = totalFailed,
                Status__c          = totalFailed == 0 ? 'Success' : 'Completed with Errors',
                Summary__c         = summary.left(32000),
                Dry_Run__c         = dryRun
            );
            insert log;
        } catch (Exception e) {
            AuditLogService.logError(LOG_SOURCE, 'writeMigrationLog', e.getMessage(), null);
        }
    }

    private void sendCompletionNotification(String summary) {
        try {
            Messaging.SingleEmailMessage mail = new Messaging.SingleEmailMessage();
            mail.setToAddresses(new List<String>{'migration-ops@company.com'});
            mail.setSubject('Migration Batch Complete: ' + migrationBatchLabel);
            mail.setPlainTextBody(summary + '\n\nError Log:\n' + String.join(errorLog, '\n'));
            Messaging.sendEmail(new List<Messaging.SingleEmailMessage>{mail});
        } catch (Exception e) {
            AuditLogService.logError(LOG_SOURCE, 'sendCompletionNotification', e.getMessage(), null);
        }
    }

    private String generateRunId() {
        return 'RUN-' + String.valueOf(Crypto.getRandomInteger()).replace('-','')
            + '-' + DateTime.now().format('yyyyMMddHHmmss');
    }
}
