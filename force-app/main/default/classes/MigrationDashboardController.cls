/**
 * @description  Apex controller for the Migration Dashboard LWC.
 *               All methods are @AuraEnabled(cacheable=false) since data changes frequently.
 *               Queries MigrationLog__c and queries siebel migration_status fields on standard objects.
 * @author       Migration Platform Team
 * @lastModified 2026-03-17
 */
public with sharing class MigrationDashboardController {

    /** ─── Stats KPI card ─────────────────────────────────────────────── */
    @AuraEnabled(cacheable=false)
    public static Map<String, Object> getMigrationStats() {
        Map<String, Object> stats = new Map<String, Object>();

        // Total, migrated, failed, pending counts from Account
        Integer totalAccounts    = [SELECT COUNT() FROM Account];
        Integer migratedAccounts = [SELECT COUNT() FROM Account WHERE Migration_Status__c = 'Migrated'];
        Integer failedAccounts   = [SELECT COUNT() FROM Account WHERE Migration_Status__c = 'Failed'];
        Integer pendingAccounts  = [SELECT COUNT() FROM Account WHERE Migration_Status__c = 'Pending'];

        // Total across all objects (Account + Contact + Opportunity)
        Integer totalContacts    = [SELECT COUNT() FROM Contact];
        Integer totalOpps        = [SELECT COUNT() FROM Opportunity];
        Integer grandTotal       = totalAccounts + totalContacts + totalOpps;

        Integer totalMigrated    = migratedAccounts
            + [SELECT COUNT() FROM Contact WHERE Migration_Status__c = 'Migrated']
            + [SELECT COUNT() FROM Opportunity WHERE Migration_Status__c = 'Migrated'];

        Integer totalFailed      = failedAccounts
            + [SELECT COUNT() FROM Contact WHERE Migration_Status__c = 'Failed']
            + [SELECT COUNT() FROM Opportunity WHERE Migration_Status__c = 'Failed'];

        Integer inProgress       = [SELECT COUNT() FROM Account WHERE Migration_Status__c = 'In Progress']
            + [SELECT COUNT() FROM Contact WHERE Migration_Status__c = 'In Progress']
            + [SELECT COUNT() FROM Opportunity WHERE Migration_Status__c = 'In Progress'];

        Integer totalPending     = grandTotal - totalMigrated - totalFailed - inProgress;

        Decimal successRate = grandTotal > 0
            ? ((Decimal) totalMigrated / grandTotal * 100).setScale(1)
            : 0;
        Decimal failureRate = grandTotal > 0
            ? ((Decimal) totalFailed / grandTotal * 100).setScale(1)
            : 0;

        stats.put('totalRecords',        grandTotal);
        stats.put('migratedRecords',     totalMigrated);
        stats.put('failedRecords',       totalFailed);
        stats.put('inProgressRecords',   inProgress);
        stats.put('pendingRecords',      totalPending);
        stats.put('successRate',         successRate);
        stats.put('failureRate',         failureRate);
        stats.put('estimatedCompletion', estimateCompletion(grandTotal, totalMigrated, inProgress));

        return stats;
    }

    /** ─── Object breakdown for progress rows ───────────────────────── */
    @AuraEnabled(cacheable=false)
    public static List<Map<String, Object>> getObjectBreakdown() {
        List<Map<String, Object>> rows = new List<Map<String, Object>>();
        List<String[]> objects = new List<String[]>{
            new String[]{'Account',     'Accounts'},
            new String[]{'Contact',     'Contacts'},
            new String[]{'Opportunity', 'Opportunities'}
        };
        for (String[] obj : objects) {
            String apiName   = obj[0];
            String label     = obj[1];
            String countSoql = 'SELECT COUNT() FROM ' + apiName;
            String migSoql   = 'SELECT COUNT() FROM ' + apiName + ' WHERE Migration_Status__c = \'Migrated\'';
            String failSoql  = 'SELECT COUNT() FROM ' + apiName + ' WHERE Migration_Status__c = \'Failed\'';
            Integer total    = Database.countQuery(countSoql);
            Integer migrated = Database.countQuery(migSoql);
            Integer failed   = Database.countQuery(failSoql);
            rows.add(new Map<String, Object>{
                'label'   => label,
                'total'   => total,
                'migrated'=> migrated,
                'failed'  => failed,
            });
        }
        return rows;
    }

    /** ─── Recent batch runs from MigrationLog__c ─────────────────── */
    @AuraEnabled(cacheable=false)
    public static List<Map<String, Object>> getRecentBatchRuns(Integer maxRecords) {
        Integer lim = maxRecords != null ? maxRecords : 8;
        List<MigrationLog__c> logs = [
            SELECT Name, Status__c, Object_Name__c, Total_Processed__c,
                   Total_Success__c, Total_Failed__c, Duration_Minutes__c,
                   Start_Time__c, Dry_Run__c
            FROM MigrationLog__c
            ORDER BY CreatedDate DESC
            LIMIT :lim
        ];
        List<Map<String, Object>> result = new List<Map<String, Object>>();
        for (MigrationLog__c log : logs) {
            String durationStr = log.Duration_Minutes__c != null
                ? String.valueOf(log.Duration_Minutes__c.setScale(1)) + ' min'
                : '—';
            result.add(new Map<String, Object>{
                'id'       => log.Id,
                'label'    => log.Object_Name__c + (log.Dry_Run__c ? ' (dry-run)' : ''),
                'status'   => log.Status__c,
                'processed'=> (Integer)(log.Total_Processed__c != null ? log.Total_Processed__c : 0),
                'failed'   => (Integer)(log.Total_Failed__c != null ? log.Total_Failed__c : 0),
                'duration' => durationStr,
            });
        }
        return result;
    }

    /** ─── Recent errors ──────────────────────────────────────────────── */
    @AuraEnabled(cacheable=false)
    public static List<Map<String, Object>> getRecentErrors(Integer pageNumber, Integer pageSize) {
        Integer ps     = pageSize != null ? pageSize : 25;
        Integer offset = ((pageNumber != null ? pageNumber : 1) - 1) * ps;
        // Migration errors are stored as custom objects in real impl;
        // for now we query MigrationLog__c with Failed status as placeholder
        List<MigrationLog__c> failedLogs = [
            SELECT Name, Object_Name__c, Run_Id__c, Summary__c, Start_Time__c
            FROM MigrationLog__c
            WHERE Status__c IN ('Failed', 'Completed with Errors')
            ORDER BY CreatedDate DESC
            LIMIT :ps OFFSET :offset
        ];
        List<Map<String, Object>> rows = new List<Map<String, Object>>();
        for (MigrationLog__c log : failedLogs) {
            rows.add(new Map<String, Object>{
                'id'          => log.Id,
                'legacyId'    => log.Run_Id__c,
                'objectType'  => log.Object_Name__c,
                'errorCode'   => 'BATCH_FAILURE',
                'errorMessage'=> log.Summary__c,
                'batchId'     => log.Name,
                'createdAt'   => log.Start_Time__c,
                'isRetryable' => true,
            });
        }
        return rows;
    }

    /** ─── Launch Apex Batch ─────────────────────────────────────────── */
    @AuraEnabled(cacheable=false)
    public static String runMigrationBatch(String objectName, Integer batchSize, Boolean dryRun) {
        if (String.isBlank(objectName)) throw new AuraHandledException('objectName is required');
        if (batchSize == null || batchSize < 1 || batchSize > 2000) batchSize = 200;
        String jobId = Database.executeBatch(
            new MigrationBatchProcessor(objectName, batchSize, dryRun == true),
            batchSize
        );
        return jobId;
    }

    /** ─── Helpers ───────────────────────────────────────────────────── */
    private static String estimateCompletion(Integer total, Integer migrated, Integer inProgress) {
        if (total == 0 || migrated >= total) return 'Complete';
        if (inProgress == 0) return 'Not started';
        Integer remaining      = total - migrated;
        Decimal ratePerMinute  = inProgress > 0 ? inProgress : 1;
        Integer minutesLeft    = (Integer)(remaining / ratePerMinute);
        if (minutesLeft < 60) return minutesLeft + ' min remaining';
        return (minutesLeft / 60) + 'h ' + Math.mod(minutesLeft, 60) + 'm remaining';
    }
}
