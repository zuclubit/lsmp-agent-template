/**
 * @description       : Test class for AccountService
 *                      Covers positive, negative, bulk, and governor-limit scenarios.
 *                      Target: >= 90% code coverage
 * @author            : Migration Platform Team
 * @group             : Core Services Tests
 * @last modified on  : 2026-03-16
 **/
@isTest
private class AccountService_Test {

    // ─── Test Data Setup ─────────────────────────────────────────────────────

    @TestSetup
    static void makeData() {
        // Insert a baseline Account for read/update/deactivate tests
        Account baseline = new Account(
            Name                = 'Test Corp',
            Type                = 'Customer',
            Industry            = 'Technology',
            Phone               = '415-555-0100',
            Legacy_ID__c        = 'LEGACY-001',
            Migration_Status__c = 'Pending',
            Active__c           = true
        );
        insert baseline;
    }

    // ─── Helpers ─────────────────────────────────────────────────────────────

    private static Account getTestAccount() {
        return [SELECT Id, Name, Legacy_ID__c, Migration_Status__c, Active__c,
                       Type, Industry, Phone
                FROM   Account
                WHERE  Legacy_ID__c = 'LEGACY-001'
                LIMIT  1];
    }

    private static Account buildNewAccount(String legacyId, String name) {
        return new Account(
            Name         = name,
            Legacy_ID__c = legacyId,
            Type         = 'Prospect',
            Industry     = 'Finance',
            Phone        = '800-555-0001'
        );
    }

    // =========================================================================
    // CREATE TESTS
    // =========================================================================

    @isTest
    static void testCreateAccount_Positive() {
        Account newAcc = buildNewAccount('LEGACY-NEW-001', 'New Test Account');

        Test.startTest();
        Id createdId = AccountService.createAccount(newAcc);
        Test.stopTest();

        System.assertNotEquals(null, createdId, 'Created Account Id should not be null.');
        Account fetched = [SELECT Name, Type, Migration_Status__c FROM Account WHERE Id = :createdId];
        System.assertEquals('New Test Account', fetched.Name);
        System.assertEquals('Prospect', fetched.Type);
        System.assertEquals('Pending', fetched.Migration_Status__c,
            'Default status should be applied.');
    }

    @isTest
    static void testCreateAccount_NullRecord_ThrowsException() {
        Test.startTest();
        try {
            AccountService.createAccount(null);
            System.assert(false, 'Expected AccountServiceException was not thrown.');
        } catch (AccountService.AccountServiceException e) {
            System.assert(e.getMessage().contains('null'),
                'Exception message should mention null.');
        }
        Test.stopTest();
    }

    @isTest
    static void testCreateAccount_BlankName_ThrowsException() {
        Account acc = new Account(Name = '', Legacy_ID__c = 'LEGACY-BLANK');
        Test.startTest();
        try {
            AccountService.createAccount(acc);
            System.assert(false, 'Expected AccountServiceException was not thrown.');
        } catch (AccountService.AccountServiceException e) {
            System.assert(e.getMessage().contains('Name is required'), e.getMessage());
        }
        Test.stopTest();
    }

    @isTest
    static void testCreateAccount_WithExistingId_ThrowsException() {
        Account existing = getTestAccount();
        Test.startTest();
        try {
            AccountService.createAccount(existing);
            System.assert(false, 'Expected AccountServiceException was not thrown.');
        } catch (AccountService.AccountServiceException e) {
            System.assert(e.getMessage().contains('must be null'), e.getMessage());
        }
        Test.stopTest();
    }

    // =========================================================================
    // READ TESTS
    // =========================================================================

    @isTest
    static void testGetAccountById_Positive() {
        Account existing = getTestAccount();
        Test.startTest();
        Account result = AccountService.getAccountById(existing.Id);
        Test.stopTest();

        System.assertNotEquals(null, result, 'Account should be returned.');
        System.assertEquals('Test Corp', result.Name);
        System.assertEquals('LEGACY-001', result.Legacy_ID__c);
    }

    @isTest
    static void testGetAccountById_NotFound_ReturnsNull() {
        // Create and delete to get a "missing" Id
        Account temp = new Account(Name='Temp', Legacy_ID__c='LEGACY-TEMP-99');
        insert temp;
        Id tempId = temp.Id;
        delete temp;

        Test.startTest();
        Account result = AccountService.getAccountById(tempId);
        Test.stopTest();

        System.assertEquals(null, result, 'Should return null for deleted Account.');
    }

    @isTest
    static void testGetAccountById_NullId_ThrowsException() {
        Test.startTest();
        try {
            AccountService.getAccountById(null);
            System.assert(false, 'Expected AccountServiceException.');
        } catch (AccountService.AccountServiceException e) {
            System.assert(e.getMessage().contains('null'), e.getMessage());
        }
        Test.stopTest();
    }

    @isTest
    static void testGetAccountsByLegacyIds_Positive() {
        Set<String> ids = new Set<String>{'LEGACY-001'};
        Test.startTest();
        Map<String, Account> result = AccountService.getAccountsByLegacyIds(ids);
        Test.stopTest();

        System.assertEquals(1, result.size());
        System.assert(result.containsKey('LEGACY-001'));
    }

    @isTest
    static void testGetAccountsByLegacyIds_EmptySet_ReturnsEmptyMap() {
        Test.startTest();
        Map<String, Account> result = AccountService.getAccountsByLegacyIds(new Set<String>());
        Test.stopTest();
        System.assertEquals(0, result.size());
    }

    // =========================================================================
    // UPDATE TESTS
    // =========================================================================

    @isTest
    static void testUpdateAccount_Positive() {
        Account existing = getTestAccount();
        existing.Phone = '415-999-0000';
        existing.Type  = 'Customer - Direct';

        Test.startTest();
        Boolean result = AccountService.updateAccount(existing);
        Test.stopTest();

        System.assert(result, 'Update should return true.');
        Account updated = [SELECT Phone, Type FROM Account WHERE Id = :existing.Id];
        System.assertEquals('415-999-0000', updated.Phone);
        System.assertEquals('Customer - Direct', updated.Type);
    }

    @isTest
    static void testUpdateAccount_NullId_ThrowsException() {
        Account acc = new Account(Name='No Id');
        Test.startTest();
        try {
            AccountService.updateAccount(acc);
            System.assert(false, 'Expected exception.');
        } catch (AccountService.AccountServiceException e) {
            System.assert(e.getMessage().contains('valid Id'), e.getMessage());
        }
        Test.stopTest();
    }

    // =========================================================================
    // BULK UPSERT TESTS
    // =========================================================================

    @isTest
    static void testBulkUpsertAccounts_Insert_Positive() {
        List<Account> accounts = new List<Account>();
        for (Integer i = 1; i <= 50; i++) {
            accounts.add(buildNewAccount('BULK-NEW-' + i, 'Bulk Account ' + i));
        }

        Test.startTest();
        AccountService.BulkUpsertResult result = AccountService.bulkUpsertAccounts(accounts);
        Test.stopTest();

        System.assertEquals(50, result.successCount, 'All 50 records should succeed.');
        System.assertEquals(0,  result.failureCount);
        System.assertEquals(50, result.insertedCount);
        System.assertEquals(0,  result.updatedCount);
        System.assert(!result.hasFailures());
    }

    @isTest
    static void testBulkUpsertAccounts_Update_Positive() {
        // Pre-insert an account that will be "updated" by upsert
        Account existing = getTestAccount();
        List<Account> accounts = new List<Account>{
            new Account(Name='Test Corp Updated', Legacy_ID__c='LEGACY-001',
                        Type='Customer - Channel')
        };

        Test.startTest();
        AccountService.BulkUpsertResult result = AccountService.bulkUpsertAccounts(accounts);
        Test.stopTest();

        System.assertEquals(1, result.successCount);
        System.assertEquals(0, result.insertedCount);
        System.assertEquals(1, result.updatedCount);
    }

    @isTest
    static void testBulkUpsertAccounts_EmptyList_ThrowsException() {
        Test.startTest();
        try {
            AccountService.bulkUpsertAccounts(new List<Account>());
            System.assert(false, 'Expected exception for empty list.');
        } catch (AccountService.AccountServiceException e) {
            System.assert(e.getMessage().contains('null or empty'), e.getMessage());
        }
        Test.stopTest();
    }

    @isTest
    static void testBulkUpsertAccounts_ExceedsMax_ThrowsException() {
        List<Account> huge = new List<Account>();
        for (Integer i = 0; i <= 10001; i++) {
            huge.add(new Account(Name='Acc ' + i, Legacy_ID__c='OVER-' + i));
        }
        Test.startTest();
        try {
            AccountService.bulkUpsertAccounts(huge);
            System.assert(false, 'Expected exception for oversized list.');
        } catch (AccountService.AccountServiceException e) {
            System.assert(e.getMessage().contains('10000'), e.getMessage());
        }
        Test.stopTest();
    }

    @isTest
    static void testBulkUpsertAccounts_Chunking() {
        // Insert 250 records to exercise the chunkList logic (> DML_CHUNK_SIZE=200)
        List<Account> accounts = new List<Account>();
        for (Integer i = 1; i <= 250; i++) {
            accounts.add(buildNewAccount('CHUNK-' + i, 'Chunk Account ' + i));
        }

        Test.startTest();
        AccountService.BulkUpsertResult result = AccountService.bulkUpsertAccounts(accounts);
        Test.stopTest();

        System.assertEquals(250, result.successCount);
        System.assertEquals(100d, result.successRate());
    }

    // =========================================================================
    // DEACTIVATE TESTS
    // =========================================================================

    @isTest
    static void testDeactivateAccount_Positive() {
        Account existing = getTestAccount();

        Test.startTest();
        Boolean result = AccountService.deactivateAccount(existing.Id);
        Test.stopTest();

        System.assert(result, 'Deactivate should return true.');
        Account updated = [SELECT Active__c, Migration_Status__c FROM Account WHERE Id = :existing.Id];
        System.assertEquals(false, updated.Active__c);
        System.assertEquals('Deactivated', updated.Migration_Status__c);
    }

    @isTest
    static void testDeactivateAccount_NullId_ThrowsException() {
        Test.startTest();
        try {
            AccountService.deactivateAccount(null);
            System.assert(false, 'Expected exception for null id.');
        } catch (AccountService.AccountServiceException e) {
            System.assert(e.getMessage().contains('required'), e.getMessage());
        }
        Test.stopTest();
    }

    // =========================================================================
    // HELPER METHOD TESTS
    // =========================================================================

    @isTest
    static void testChunkList_ExactMultiple() {
        List<Account> accs = new List<Account>();
        for (Integer i = 0; i < 400; i++) {
            accs.add(new Account(Name = 'Acc ' + i));
        }
        List<List<Account>> chunks = AccountService.chunkList(accs, 200);
        System.assertEquals(2, chunks.size());
        System.assertEquals(200, chunks[0].size());
        System.assertEquals(200, chunks[1].size());
    }

    @isTest
    static void testChunkList_Remainder() {
        List<Account> accs = new List<Account>();
        for (Integer i = 0; i < 210; i++) {
            accs.add(new Account(Name = 'Acc ' + i));
        }
        List<List<Account>> chunks = AccountService.chunkList(accs, 200);
        System.assertEquals(2, chunks.size());
        System.assertEquals(200, chunks[0].size());
        System.assertEquals(10,  chunks[1].size());
    }

    // =========================================================================
    // SUCCESS RATE TEST
    // =========================================================================

    @isTest
    static void testBulkUpsertResult_SuccessRate_ZeroTotal() {
        AccountService.BulkUpsertResult r = new AccountService.BulkUpsertResult();
        System.assertEquals(0, r.successRate(), 'Zero total should give 0% rate.');
    }
}
