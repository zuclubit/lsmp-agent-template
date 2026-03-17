/**
 * @description       : Account Trigger Handler — implements all trigger event methods.
 *                      Follows the single-responsibility principle; each method handles
 *                      one event type and delegates to focused private methods.
 * @author            : Migration Platform Team
 * @group             : Triggers
 * @last modified on  : 2026-03-16
 **/
public with sharing class AccountTriggerHandler {

    private static final String LOG_SOURCE = 'AccountTriggerHandler';

    // Prevent recursion within the same transaction
    private static Boolean isExecuting = false;

    // =========================================================================
    // BEFORE INSERT
    // =========================================================================

    /**
     * @description Runs before new Account records are committed.
     *              - Validates required fields
     *              - Applies field defaults
     *              - Normalizes phone numbers
     *              - Generates internal reference numbers
     * @param newList  Trigger.new records
     */
    public void beforeInsert(List<Account> newList) {
        setMigrationDefaults(newList);
        normalizePhones(newList);
        validateRequiredFields(newList);
        assignAccountNumbers(newList);
    }

    // =========================================================================
    // BEFORE UPDATE
    // =========================================================================

    /**
     * @description Runs before Account records are updated.
     *              - Prevents locked-field edits during active migration
     *              - Re-normalizes phone numbers on change
     *              - Tracks status transitions
     * @param newMap  Trigger.newMap
     * @param oldMap  Trigger.oldMap
     */
    public void beforeUpdate(Map<Id, Account> newMap, Map<Id, Account> oldMap) {
        List<Account> phonesChanged     = new List<Account>();
        List<Account> statusTransitions = new List<Account>();

        for (Id accountId : newMap.keySet()) {
            Account newAcc = newMap.get(accountId);
            Account oldAcc = oldMap.get(accountId);

            if (newAcc.Phone != oldAcc.Phone) {
                phonesChanged.add(newAcc);
            }
            if (newAcc.Migration_Status__c != oldAcc.Migration_Status__c) {
                statusTransitions.add(newAcc);
            }
            // Prevent edits to migration-locked records (except by system user)
            if (oldAcc.Migration_Status__c == 'In Progress'
                    && !hasMigrationBypassPermission()) {
                newAcc.addError(
                    'This Account is locked during active migration. '
                    + 'Please contact the migration team.');
            }
        }

        if (!phonesChanged.isEmpty()) {
            normalizePhones(phonesChanged);
        }
        if (!statusTransitions.isEmpty()) {
            stampStatusTransitionDates(statusTransitions, oldMap);
        }
    }

    // =========================================================================
    // BEFORE DELETE
    // =========================================================================

    /**
     * @description Prevents deletion of Accounts that are being migrated or
     *              have active child records.
     * @param oldMap  Trigger.oldMap
     */
    public void beforeDelete(Map<Id, Account> oldMap) {
        for (Account acc : oldMap.values()) {
            if (acc.Migration_Status__c == 'In Progress') {
                acc.addError(
                    'Deletion blocked: Account is currently being migrated. '
                    + 'Complete or abort migration before deleting.');
            }
        }
        preventDeleteWithActiveContacts(oldMap);
    }

    // =========================================================================
    // AFTER INSERT
    // =========================================================================

    /**
     * @description Runs after new Accounts are committed.
     *              - Creates default child records
     *              - Enqueues async follow-up jobs
     *              - Logs creation events
     * @param newMap  Trigger.newMap
     */
    public void afterInsert(Map<Id, Account> newMap) {
        if (isExecuting) { return; }
        isExecuting = true;
        try {
            logAccountEvents(newMap.values(), 'CREATED');
            createDefaultOpportunityDraft(newMap.values());
        } finally {
            isExecuting = false;
        }
    }

    // =========================================================================
    // AFTER UPDATE
    // =========================================================================

    /**
     * @description Runs after Accounts are updated.
     *              - Cascades status changes to child objects
     *              - Fires integration events for external systems
     *              - Logs significant field changes
     * @param newMap  Trigger.newMap
     * @param oldMap  Trigger.oldMap
     */
    public void afterUpdate(Map<Id, Account> newMap, Map<Id, Account> oldMap) {
        if (isExecuting) { return; }
        isExecuting = true;
        try {
            List<Account> migrationCompleted = new List<Account>();
            List<Account> deactivated        = new List<Account>();
            Set<Id>       billingChanged     = new Set<Id>();

            for (Id accountId : newMap.keySet()) {
                Account newAcc = newMap.get(accountId);
                Account oldAcc = oldMap.get(accountId);

                if (newAcc.Migration_Status__c == 'Migrated'
                        && oldAcc.Migration_Status__c != 'Migrated') {
                    migrationCompleted.add(newAcc);
                }
                if (newAcc.Active__c == false && oldAcc.Active__c == true) {
                    deactivated.add(newAcc);
                }
                if (newAcc.BillingStreet != oldAcc.BillingStreet
                        || newAcc.BillingCity != oldAcc.BillingCity
                        || newAcc.BillingPostalCode != oldAcc.BillingPostalCode) {
                    billingChanged.add(accountId);
                }
            }

            if (!migrationCompleted.isEmpty()) {
                handleMigrationCompletion(migrationCompleted);
            }
            if (!deactivated.isEmpty()) {
                logAccountEvents(deactivated, 'DEACTIVATED');
            }
            if (!billingChanged.isEmpty()) {
                System.enqueueJob(new AddressVerificationQueueable(billingChanged));
            }
        } finally {
            isExecuting = false;
        }
    }

    // =========================================================================
    // AFTER DELETE
    // =========================================================================

    /**
     * @description Logs Account deletion events for compliance audit trail.
     * @param oldMap  Trigger.oldMap
     */
    public void afterDelete(Map<Id, Account> oldMap) {
        logAccountEvents(oldMap.values(), 'DELETED');
    }

    // =========================================================================
    // AFTER UNDELETE
    // =========================================================================

    /**
     * @description Resets migration status on undeleted Accounts.
     * @param newMap  Trigger.newMap (restored records)
     */
    public void afterUndelete(Map<Id, Account> newMap) {
        List<Account> toUpdate = new List<Account>();
        for (Account acc : newMap.values()) {
            if (acc.Migration_Status__c == 'Deactivated') {
                toUpdate.add(new Account(
                    Id                  = acc.Id,
                    Migration_Status__c = 'Pending',
                    Active__c           = true
                ));
            }
        }
        if (!toUpdate.isEmpty()) {
            Database.update(toUpdate, false);
        }
        logAccountEvents(newMap.values(), 'UNDELETED');
    }

    // =========================================================================
    // PRIVATE METHODS
    // =========================================================================

    private void setMigrationDefaults(List<Account> records) {
        for (Account acc : records) {
            if (acc.Migration_Status__c == null) {
                acc.Migration_Status__c = 'Pending';
            }
            if (acc.Active__c == null) {
                acc.Active__c = true;
            }
            if (String.isBlank(acc.Type)) {
                acc.Type = 'Prospect';
            }
        }
    }

    private void normalizePhones(List<Account> records) {
        for (Account acc : records) {
            if (String.isNotBlank(acc.Phone)) {
                String digits = acc.Phone.replaceAll('[^0-9]', '');
                if (digits.length() == 10) {
                    acc.Phone = '(' + digits.substring(0,3) + ') '
                        + digits.substring(3,6) + '-' + digits.substring(6);
                }
            }
        }
    }

    private void validateRequiredFields(List<Account> records) {
        for (Account acc : records) {
            if (String.isBlank(acc.Name)) {
                acc.Name.addError('Account Name is required.');
            }
        }
    }

    private void assignAccountNumbers(List<Account> records) {
        for (Account acc : records) {
            if (String.isBlank(acc.AccountNumber) && String.isNotBlank(acc.Legacy_ID__c)) {
                acc.AccountNumber = 'ACC-' + acc.Legacy_ID__c;
            }
        }
    }

    private void stampStatusTransitionDates(List<Account> records, Map<Id, Account> oldMap) {
        for (Account acc : records) {
            if (acc.Migration_Status__c == 'Migrated') {
                acc.Migration_Completed_Date__c = System.now();
            } else if (acc.Migration_Status__c == 'Failed') {
                acc.Migration_Failed_Date__c = System.now();
            }
        }
    }

    private void preventDeleteWithActiveContacts(Map<Id, Account> oldMap) {
        Map<Id, Integer> contactCounts = new Map<Id, Integer>();
        for (AggregateResult ar : [
            SELECT AccountId, COUNT(Id) cnt
            FROM   Contact
            WHERE  AccountId IN :oldMap.keySet()
            AND    IsActive__c = true
            GROUP BY AccountId
        ]) {
            contactCounts.put((Id)ar.get('AccountId'), (Integer)ar.get('cnt'));
        }
        for (Id accId : contactCounts.keySet()) {
            if (contactCounts.get(accId) > 0) {
                oldMap.get(accId).addError(
                    'Cannot delete Account with ' + contactCounts.get(accId)
                    + ' active Contact(s). Deactivate contacts first.');
            }
        }
    }

    private void logAccountEvents(List<Account> records, String event) {
        for (Account acc : records) {
            AuditLogService.logSuccess(LOG_SOURCE, event,
                event + ': ' + acc.Name + ' (Id=' + acc.Id + ')', acc.Id);
        }
        AuditLogService.flushBuffer();
    }

    private void handleMigrationCompletion(List<Account> accounts) {
        for (Account acc : accounts) {
            AuditLogService.logSuccess(LOG_SOURCE, 'MIGRATION_COMPLETE',
                'Account migration completed: ' + acc.Name
                + ' LegacyId=' + acc.Legacy_ID__c, acc.Id);
        }
        AuditLogService.flushBuffer();
    }

    private void createDefaultOpportunityDraft(List<Account> accounts) {
        // Only create draft Opps for new Customer-type accounts
        List<Opportunity> opps = new List<Opportunity>();
        for (Account acc : accounts) {
            if (acc.Type != null && acc.Type.startsWith('Customer')) {
                opps.add(new Opportunity(
                    Name        = acc.Name + ' - Initial Opportunity',
                    AccountId   = acc.Id,
                    StageName   = 'Prospecting',
                    CloseDate   = Date.today().addDays(90),
                    Description = 'Auto-created during migration onboarding.'
                ));
            }
        }
        if (!opps.isEmpty()) {
            Database.insert(opps, false);
        }
    }

    private Boolean hasMigrationBypassPermission() {
        return FeatureManagement.checkPermission('Migration_Bypass_Lock');
    }

    // ─── Queueable for async address verification ─────────────────────────────
    private class AddressVerificationQueueable implements Queueable, Database.AllowsCallouts {
        private Set<Id> accountIds;
        AddressVerificationQueueable(Set<Id> ids) { this.accountIds = ids; }

        public void execute(QueueableContext ctx) {
            // Validate addresses via IntegrationService
            for (Account acc : [
                SELECT Id, BillingStreet, BillingCity, BillingState,
                       BillingPostalCode, BillingCountry
                FROM   Account
                WHERE  Id IN :accountIds
            ]) {
                try {
                    Map<String, String> validated = IntegrationService.validateAddress(
                        acc.BillingStreet, acc.BillingCity, acc.BillingState,
                        acc.BillingPostalCode, acc.BillingCountry);

                    if (validated.get('validated') == 'true') {
                        Account upd = new Account(
                            Id                  = acc.Id,
                            BillingStreet       = validated.get('street'),
                            BillingCity         = validated.get('city'),
                            BillingState        = validated.get('state'),
                            BillingPostalCode   = validated.get('zip'),
                            BillingCountry      = validated.get('country'),
                            Address_Verified__c = true
                        );
                        Database.update(upd, false);
                    }
                } catch (Exception e) {
                    AuditLogService.logError(LOG_SOURCE,
                        'AddressVerificationQueueable', e.getMessage(), acc.Id);
                }
            }
        }
    }
}
