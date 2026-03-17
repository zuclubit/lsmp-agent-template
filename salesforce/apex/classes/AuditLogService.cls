/**
 * @description       : Centralized audit logging service.
 *                      Writes structured log entries to the MigrationLog__c custom object
 *                      and System.debug. Designed to be called from any service layer.
 *                      Uses a platform-event-style buffer to minimize DML operations.
 * @author            : Migration Platform Team
 * @group             : Core Services
 * @last modified on  : 2026-03-16
 **/
public without sharing class AuditLogService {

    // ─── Log Level Enum ──────────────────────────────────────────────────────
    public enum LogLevel { DEBUG_LOG, INFO, WARNING, ERROR, CRITICAL }

    // ─── Custom Exception ────────────────────────────────────────────────────
    public class AuditLogException extends Exception {}

    // ─── Constants ───────────────────────────────────────────────────────────
    private static final Integer MAX_BUFFER_SIZE   = 100;
    private static final Integer MAX_TEXT_LENGTH   = 32768;
    private static final String  DEFAULT_SOURCE    = 'Unknown';

    // ─── In-memory buffer (flushed on demand or at transaction end) ──────────
    @TestVisible
    private static List<Audit_Log__c> logBuffer = new List<Audit_Log__c>();

    // ─── Singleton user context (cached for performance) ─────────────────────
    private static String currentUserId;
    private static String currentUserName;

    static {
        currentUserId   = UserInfo.getUserId();
        currentUserName = UserInfo.getName();
    }

    // =========================================================================
    // PUBLIC CONVENIENCE METHODS
    // =========================================================================

    /**
     * @description Logs an informational success event.
     * @param source     Originating class/component name
     * @param action     Method or action name
     * @param message    Human-readable log message
     * @param recordId   Related record Id (optional)
     */
    public static void logSuccess(String source, String action,
                                   String message, Id recordId) {
        log(LogLevel.INFO, source, action, message, null, null, recordId);
    }

    /**
     * @description Logs an error event.
     * @param source     Originating class/component name
     * @param action     Method or action name
     * @param message    Error description
     * @param recordId   Related record Id (optional)
     */
    public static void logError(String source, String action,
                                  String message, Id recordId) {
        log(LogLevel.ERROR, source, action, message, null, null, recordId);
    }

    /**
     * @description Logs a warning event.
     */
    public static void logWarning(String source, String action,
                                    String message, Id recordId) {
        log(LogLevel.WARNING, source, action, message, null, null, recordId);
    }

    /**
     * @description Logs a data-change event with old and new value snapshots.
     * @param source    Originating class name
     * @param action    Action performed
     * @param recordId  Id of the changed record
     * @param oldValues JSON or text snapshot of previous state
     * @param newValues JSON or text snapshot of new state
     */
    public static void logChange(String source, String action,
                                   Id recordId, String oldValues, String newValues) {
        log(LogLevel.INFO, source, action, 'Record updated: ' + recordId,
            oldValues, newValues, recordId);
    }

    /**
     * @description Logs a critical system alert and immediately flushes buffer.
     */
    public static void logCritical(String source, String action,
                                     String message, Id recordId) {
        log(LogLevel.CRITICAL, source, action, message, null, null, recordId);
        flushBuffer(); // Immediately persist critical events
    }

    /**
     * @description Full-parameter log entry.
     * @param level     Log severity level
     * @param source    Originating component
     * @param action    Operation name
     * @param message   Log message
     * @param oldValues Previous field values (change events only)
     * @param newValues New field values (change events only)
     * @param recordId  Related Salesforce record Id
     */
    public static void log(LogLevel level, String source, String action,
                             String message, String oldValues, String newValues,
                             Id recordId) {
        // Always emit to debug log regardless of DML availability
        String debugLine = '[' + level.name() + '] '
            + (source  != null ? source  : DEFAULT_SOURCE) + '.'
            + (action  != null ? action  : 'unknown') + ' | '
            + truncate(message, 500);
        System.debug(resolveLoggingLevel(level), debugLine);

        Audit_Log__c entry = new Audit_Log__c(
            Log_Level__c     = level.name(),
            Source__c        = truncate(source != null ? source : DEFAULT_SOURCE, 255),
            Action__c        = truncate(action != null ? action : 'unknown', 255),
            Message__c       = truncate(message, MAX_TEXT_LENGTH),
            Old_Values__c    = truncate(oldValues, MAX_TEXT_LENGTH),
            New_Values__c    = truncate(newValues, MAX_TEXT_LENGTH),
            Related_Record_Id__c = recordId != null ? String.valueOf(recordId) : null,
            User_Id__c       = currentUserId,
            User_Name__c     = currentUserName,
            Transaction_Id__c= Request.getCurrent().getRequestId(),
            Org_Id__c        = UserInfo.getOrganizationId(),
            Timestamp__c     = System.now()
        );

        logBuffer.add(entry);

        // Auto-flush when buffer is full
        if (logBuffer.size() >= MAX_BUFFER_SIZE) {
            flushBuffer();
        }
    }

    // =========================================================================
    // BUFFER MANAGEMENT
    // =========================================================================

    /**
     * @description Persists all buffered log entries to the database.
     *              Safe to call multiple times — clears buffer after each flush.
     */
    public static void flushBuffer() {
        if (logBuffer.isEmpty()) { return; }

        List<Audit_Log__c> toInsert = logBuffer.clone();
        logBuffer.clear();

        try {
            // Use all-or-nothing false to avoid one bad record blocking others
            Database.insert(toInsert, false);
        } catch (Exception e) {
            // Last resort: write to System.debug so logs are never fully lost
            System.debug(LoggingLevel.ERROR,
                'AuditLogService.flushBuffer failed: ' + e.getMessage()
                + '. Lost ' + toInsert.size() + ' log entries.');
        }
    }

    /**
     * @description Returns a copy of the current in-memory buffer (for tests).
     */
    @TestVisible
    public static List<Audit_Log__c> getBuffer() {
        return logBuffer.clone();
    }

    /**
     * @description Clears the buffer without persisting (test utility).
     */
    @TestVisible
    public static void clearBuffer() {
        logBuffer.clear();
    }

    // =========================================================================
    // QUERY METHODS
    // =========================================================================

    /**
     * @description Returns the most recent log entries for a given record.
     * @param recordId  Salesforce record Id
     * @param limitCount Maximum number of results
     * @return List of Audit_Log__c records ordered by newest first
     */
    public static List<Audit_Log__c> getLogsForRecord(Id recordId, Integer limitCount) {
        if (recordId == null) { return new List<Audit_Log__c>(); }
        Integer maxLimit = limitCount != null ? limitCount : 50;
        return [
            SELECT  Id, Log_Level__c, Source__c, Action__c, Message__c,
                    Old_Values__c, New_Values__c, User_Name__c, Timestamp__c
            FROM    Audit_Log__c
            WHERE   Related_Record_Id__c = :String.valueOf(recordId)
            ORDER BY Timestamp__c DESC
            LIMIT   :maxLimit
        ];
    }

    /**
     * @description Returns error logs for a given source component.
     * @param source     Source class name
     * @param sinceDate  Return logs after this date
     * @return List of error-level log entries
     */
    public static List<Audit_Log__c> getErrorLogs(String source, DateTime sinceDate) {
        DateTime since = sinceDate != null ? sinceDate : System.now().addDays(-7);
        return [
            SELECT  Id, Log_Level__c, Source__c, Action__c, Message__c,
                    Related_Record_Id__c, User_Name__c, Timestamp__c
            FROM    Audit_Log__c
            WHERE   Log_Level__c IN ('ERROR', 'CRITICAL')
            AND     Source__c = :source
            AND     Timestamp__c >= :since
            ORDER BY Timestamp__c DESC
            LIMIT   200
        ];
    }

    /**
     * @description Returns aggregated error counts grouped by source for the dashboard.
     * @param sinceDate  Start of reporting window
     * @return List of AggregateResult with Source__c and error_count
     */
    public static List<AggregateResult> getErrorSummary(DateTime sinceDate) {
        DateTime since = sinceDate != null ? sinceDate : System.now().addHours(-24);
        return [
            SELECT  Source__c, Log_Level__c, COUNT(Id) error_count
            FROM    Audit_Log__c
            WHERE   Log_Level__c IN ('ERROR', 'CRITICAL')
            AND     Timestamp__c >= :since
            GROUP BY Source__c, Log_Level__c
            ORDER BY COUNT(Id) DESC
            LIMIT   100
        ];
    }

    // =========================================================================
    // PRIVATE HELPERS
    // =========================================================================

    private static String truncate(String val, Integer maxLen) {
        if (val == null) { return null; }
        return val.length() > maxLen ? val.substring(0, maxLen) : val;
    }

    private static LoggingLevel resolveLoggingLevel(LogLevel level) {
        switch on level {
            when DEBUG_LOG { return LoggingLevel.DEBUG; }
            when INFO      { return LoggingLevel.INFO;  }
            when WARNING   { return LoggingLevel.WARN;  }
            when ERROR     { return LoggingLevel.ERROR; }
            when CRITICAL  { return LoggingLevel.ERROR; }
            when else      { return LoggingLevel.DEBUG; }
        }
    }
}
