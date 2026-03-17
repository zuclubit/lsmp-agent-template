/**
 * @description  : Account trigger — thin dispatcher following the one-trigger-per-object
 *                 pattern. All business logic is delegated to AccountTriggerHandler.
 *                 This trigger itself contains zero business logic.
 * @author       : Migration Platform Team
 * @group        : Triggers
 * @last modified: 2026-03-16
 */
trigger AccountTrigger on Account (
    before insert,
    before update,
    before delete,
    after  insert,
    after  update,
    after  delete,
    after  undelete
) {
    // Guard: allow bypass via custom metadata or permission (e.g., during data loads)
    if (TriggerBypassSettings__c.getInstance()?.Bypass_Account_Trigger__c == true) {
        return;
    }

    AccountTriggerHandler handler = new AccountTriggerHandler();

    switch on Trigger.operationType {

        when BEFORE_INSERT {
            handler.beforeInsert(Trigger.new);
        }
        when BEFORE_UPDATE {
            handler.beforeUpdate(Trigger.newMap, Trigger.oldMap);
        }
        when BEFORE_DELETE {
            handler.beforeDelete(Trigger.oldMap);
        }
        when AFTER_INSERT {
            handler.afterInsert(Trigger.newMap);
        }
        when AFTER_UPDATE {
            handler.afterUpdate(Trigger.newMap, Trigger.oldMap);
        }
        when AFTER_DELETE {
            handler.afterDelete(Trigger.oldMap);
        }
        when AFTER_UNDELETE {
            handler.afterUndelete(Trigger.newMap);
        }
    }
}
