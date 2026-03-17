/**
 * @description       : Enterprise Account Service for Legacy-to-Salesforce Migration
 *                      Provides full CRUD operations, bulk processing, and robust
 *                      error handling with governor limit awareness.
 * @author            : Migration Platform Team
 * @group             : Core Services
 * @last modified on  : 2026-03-16
 * @last modified by  : Migration Platform Team
 **/
public with sharing class AccountService {

    // ─── Custom Exception ────────────────────────────────────────────────────────
    public class AccountServiceException extends Exception {}

    // ─── Constants ───────────────────────────────────────────────────────────────
    private static final Integer MAX_BULK_RECORDS   = 10000;
    private static final Integer DML_CHUNK_SIZE     = 200;
    private static final String  LOG_SOURCE         = 'AccountService';
    private static final String  DEFAULT_ACCOUNT_TYPE = 'Prospect';

    // ─── Dependency-injection hooks (replaced by test stubs in unit tests) ───────
    @TestVisible private static Boolean bypassTriggers = false;

    // =========================================================================
    // CREATE
    // =========================================================================

    /**
     * @description Creates a single Account record with full validation and audit.
     * @param       accountRecord  Populated Account sObject (Id must be null)
     * @return      The Id of the newly created Account
     * @throws      AccountServiceException on validation failure or DML error
     */
    public static Id createAccount(Account accountRecord) {
        if (accountRecord == null) {
            throw new AccountServiceException('accountRecord cannot be null.');
        }
        if (String.isBlank(accountRecord.Name)) {
            throw new AccountServiceException('Account Name is required.');
        }
        if (accountRecord.Id != null) {
            throw new AccountServiceException('Account Id must be null for creation.');
        }

        accountRecord = applyDefaults(accountRecord);

        Savepoint sp = Database.setSavepoint();
        try {
            Database.SaveResult result = Database.insert(accountRecord, false);
            if (!result.isSuccess()) {
                String errMsg = buildErrorMessage(result.getErrors());
                AuditLogService.logError(LOG_SOURCE, 'createAccount', errMsg, null);
                throw new AccountServiceException('Insert failed: ' + errMsg);
            }
            AuditLogService.logSuccess(LOG_SOURCE, 'createAccount',
                'Account created: ' + result.getId(), null);
            return result.getId();
        } catch (AccountServiceException ace) {
            Database.rollback(sp);
            throw ace;
        } catch (Exception e) {
            Database.rollback(sp);
            AuditLogService.logError(LOG_SOURCE, 'createAccount', e.getMessage(), null);
            throw new AccountServiceException('Unexpected error during createAccount: '
                + e.getMessage(), e);
        }
    }

    // =========================================================================
    // READ
    // =========================================================================

    /**
     * @description Retrieves an Account by Id with standard field set.
     * @param       accountId  18-character Account Id
     * @return      Account record or null if not found
     * @throws      AccountServiceException on invalid Id format
     */
    public static Account getAccountById(Id accountId) {
        if (accountId == null) {
            throw new AccountServiceException('accountId cannot be null.');
        }
        if (accountId.getSObjectType() != Account.SObjectType) {
            throw new AccountServiceException('Provided Id is not an Account Id.');
        }

        List<Account> results = [
            SELECT  Id, Name, AccountNumber, Type, Industry, AnnualRevenue,
                    BillingStreet, BillingCity, BillingState, BillingPostalCode,
                    BillingCountry, Phone, Fax, Website, Description,
                    OwnerId, CreatedDate, LastModifiedDate,
                    Legacy_ID__c, Migration_Status__c, Migration_Batch__c
            FROM    Account
            WHERE   Id = :accountId
            WITH    SECURITY_ENFORCED
            LIMIT   1
        ];
        return results.isEmpty() ? null : results[0];
    }

    /**
     * @description Retrieves multiple Accounts by their legacy external Ids.
     * @param       legacyIds  Set of legacy system identifiers
     * @return      Map of legacyId -> Account
     */
    public static Map<String, Account> getAccountsByLegacyIds(Set<String> legacyIds) {
        if (legacyIds == null || legacyIds.isEmpty()) {
            return new Map<String, Account>();
        }

        Map<String, Account> resultMap = new Map<String, Account>();
        for (Account acc : [
            SELECT  Id, Name, Legacy_ID__c, Migration_Status__c, AccountNumber,
                    Type, Industry, Phone, BillingStreet, BillingCity,
                    BillingState, BillingPostalCode, BillingCountry
            FROM    Account
            WHERE   Legacy_ID__c IN :legacyIds
            WITH    SECURITY_ENFORCED
        ]) {
            resultMap.put(acc.Legacy_ID__c, acc);
        }
        return resultMap;
    }

    // =========================================================================
    // UPDATE
    // =========================================================================

    /**
     * @description Updates an existing Account record.
     * @param       accountRecord  Account with a valid Id and fields to update
     * @return      True if update succeeded
     * @throws      AccountServiceException on validation or DML failure
     */
    public static Boolean updateAccount(Account accountRecord) {
        if (accountRecord == null || accountRecord.Id == null) {
            throw new AccountServiceException('Account with valid Id is required for update.');
        }

        Savepoint sp = Database.setSavepoint();
        try {
            // Capture old values for audit
            Account oldRecord = getAccountById(accountRecord.Id);
            String oldValues = buildAuditSnapshot(oldRecord);

            Database.SaveResult result = Database.update(accountRecord, false);
            if (!result.isSuccess()) {
                String errMsg = buildErrorMessage(result.getErrors());
                Database.rollback(sp);
                AuditLogService.logError(LOG_SOURCE, 'updateAccount', errMsg, accountRecord.Id);
                throw new AccountServiceException('Update failed: ' + errMsg);
            }

            AuditLogService.logChange(LOG_SOURCE, 'updateAccount', accountRecord.Id,
                oldValues, buildAuditSnapshot(accountRecord));
            return true;
        } catch (AccountServiceException ace) {
            Database.rollback(sp);
            throw ace;
        } catch (Exception e) {
            Database.rollback(sp);
            AuditLogService.logError(LOG_SOURCE, 'updateAccount', e.getMessage(), accountRecord.Id);
            throw new AccountServiceException('Unexpected error during updateAccount: '
                + e.getMessage(), e);
        }
    }

    // =========================================================================
    // BULK UPSERT
    // =========================================================================

    /**
     * @description Bulk-upserts Account records using external Id field.
     *              Chunks into DML_CHUNK_SIZE slices to respect governor limits.
     * @param       accounts    List of Account records (max MAX_BULK_RECORDS)
     * @return      BulkUpsertResult wrapper with success/failure breakdown
     * @throws      AccountServiceException if list exceeds max or is empty
     */
    public static BulkUpsertResult bulkUpsertAccounts(List<Account> accounts) {
        if (accounts == null || accounts.isEmpty()) {
            throw new AccountServiceException('Accounts list cannot be null or empty.');
        }
        if (accounts.size() > MAX_BULK_RECORDS) {
            throw new AccountServiceException(
                'Bulk upsert limited to ' + MAX_BULK_RECORDS + ' records per call. '
                + 'Received: ' + accounts.size());
        }

        BulkUpsertResult bulkResult = new BulkUpsertResult();
        List<List<Account>> chunks  = chunkList(accounts, DML_CHUNK_SIZE);

        for (List<Account> chunk : chunks) {
            List<Database.UpsertResult> results =
                Database.upsert(chunk, Account.Legacy_ID__c, false);

            Integer idx = 0;
            for (Database.UpsertResult ur : results) {
                if (ur.isSuccess()) {
                    bulkResult.successIds.add(ur.getId());
                    bulkResult.successCount++;
                    if (ur.isCreated()) { bulkResult.insertedCount++; }
                    else                { bulkResult.updatedCount++;  }
                } else {
                    String errMsg = buildErrorMessage(ur.getErrors());
                    bulkResult.failures.add(new BulkUpsertFailure(
                        chunk[idx].Legacy_ID__c, chunk[idx].Name, errMsg));
                    bulkResult.failureCount++;
                }
                idx++;
            }
        }

        AuditLogService.logSuccess(LOG_SOURCE, 'bulkUpsertAccounts',
            'Bulk upsert complete. Success=' + bulkResult.successCount
            + ', Failures=' + bulkResult.failureCount, null);

        return bulkResult;
    }

    // =========================================================================
    // DEACTIVATE (soft-delete via status field)
    // =========================================================================

    /**
     * @description Soft-deactivates an Account by setting Active__c = false
     *              and Migration_Status__c = 'Deactivated'.
     * @param       accountId  Id of the Account to deactivate
     * @return      True on success
     */
    public static Boolean deactivateAccount(Id accountId) {
        if (accountId == null) {
            throw new AccountServiceException('accountId is required for deactivation.');
        }

        Account acc = getAccountById(accountId);
        if (acc == null) {
            throw new AccountServiceException('Account not found: ' + accountId);
        }

        Account updateRec = new Account(
            Id                  = accountId,
            Active__c           = false,
            Migration_Status__c = 'Deactivated'
        );

        Savepoint sp = Database.setSavepoint();
        try {
            Database.SaveResult result = Database.update(updateRec, false);
            if (!result.isSuccess()) {
                Database.rollback(sp);
                throw new AccountServiceException(
                    'Deactivation failed: ' + buildErrorMessage(result.getErrors()));
            }
            AuditLogService.logChange(LOG_SOURCE, 'deactivateAccount',
                accountId, 'Active=true', 'Active=false, Status=Deactivated');
            return true;
        } catch (AccountServiceException ace) {
            Database.rollback(sp);
            throw ace;
        } catch (Exception e) {
            Database.rollback(sp);
            AuditLogService.logError(LOG_SOURCE, 'deactivateAccount', e.getMessage(), accountId);
            throw new AccountServiceException('Unexpected error during deactivateAccount: '
                + e.getMessage(), e);
        }
    }

    // =========================================================================
    // PRIVATE HELPERS
    // =========================================================================

    private static Account applyDefaults(Account acc) {
        if (String.isBlank(acc.Type)) {
            acc.Type = DEFAULT_ACCOUNT_TYPE;
        }
        if (acc.Migration_Status__c == null) {
            acc.Migration_Status__c = 'Pending';
        }
        return acc;
    }

    private static String buildErrorMessage(List<Database.Error> errors) {
        List<String> msgs = new List<String>();
        for (Database.Error err : errors) {
            msgs.add('[' + err.getStatusCode() + '] ' + err.getMessage()
                + ' Fields: ' + String.join(err.getFields(), ', '));
        }
        return String.join(msgs, ' | ');
    }

    private static String buildAuditSnapshot(Account acc) {
        if (acc == null) { return 'null'; }
        return JSON.serialize(new Map<String, Object>{
            'Name'               => acc.Name,
            'Type'               => acc.Type,
            'Industry'           => acc.Industry,
            'Migration_Status__c'=> acc.Migration_Status__c,
            'Phone'              => acc.Phone
        });
    }

    @TestVisible
    private static List<List<Account>> chunkList(List<Account> fullList, Integer chunkSize) {
        List<List<Account>> chunks = new List<List<Account>>();
        List<Account> current      = new List<Account>();
        Integer count              = 0;
        for (Account a : fullList) {
            current.add(a);
            count++;
            if (count == chunkSize) {
                chunks.add(current);
                current = new List<Account>();
                count   = 0;
            }
        }
        if (!current.isEmpty()) { chunks.add(current); }
        return chunks;
    }

    // =========================================================================
    // RESULT WRAPPERS
    // =========================================================================

    public class BulkUpsertResult {
        public Integer       successCount  = 0;
        public Integer       failureCount  = 0;
        public Integer       insertedCount = 0;
        public Integer       updatedCount  = 0;
        public List<Id>      successIds    = new List<Id>();
        public List<BulkUpsertFailure> failures = new List<BulkUpsertFailure>();

        public Boolean hasFailures() { return failureCount > 0; }
        public Decimal successRate() {
            Integer total = successCount + failureCount;
            return total == 0 ? 0 : ((Decimal)successCount / total * 100).setScale(2);
        }
    }

    public class BulkUpsertFailure {
        public String legacyId;
        public String accountName;
        public String errorMessage;
        public BulkUpsertFailure(String legId, String name, String err) {
            this.legacyId     = legId;
            this.accountName  = name;
            this.errorMessage = err;
        }
    }
}
