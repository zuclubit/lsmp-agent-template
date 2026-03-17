/**
 * @description       : Centralized HTTP integration service for external system callouts.
 *                      Uses Named Credentials for authentication, implements exponential
 *                      backoff retry logic, structured response parsing, and full audit trail.
 * @author            : Migration Platform Team
 * @group             : Integration
 * @last modified on  : 2026-03-16
 **/
public with sharing class IntegrationService {

    // ─── Custom Exception ────────────────────────────────────────────────────
    public class IntegrationException extends Exception {}

    // ─── Constants ───────────────────────────────────────────────────────────
    private static final String  NAMED_CREDENTIAL_PREFIX = 'callout:';
    private static final Integer DEFAULT_TIMEOUT_MS      = 30000;
    private static final Integer MAX_RETRIES             = 3;
    private static final Integer BASE_RETRY_DELAY_MS     = 1000;
    private static final String  LOG_SOURCE              = 'IntegrationService';

    // ─── Named Credential Identifiers ────────────────────────────────────────
    public static final String NC_LEGACY_SYSTEM  = 'Legacy_System_API';
    public static final String NC_DATA_LAKE      = 'Data_Lake_API';
    public static final String NC_VALIDATION_SVC = 'Address_Validation_API';

    // =========================================================================
    // PUBLIC REST METHODS
    // =========================================================================

    /**
     * @description Performs an authenticated GET request with retry logic.
     * @param namedCredential  Named credential developer name
     * @param endpoint         Relative path appended to the named credential base URL
     * @param queryParams      URL query parameters (null-safe)
     * @return                 ApiResponse wrapper
     */
    public static ApiResponse doGet(String namedCredential, String endpoint,
                                     Map<String, String> queryParams) {
        String url = buildUrl(namedCredential, endpoint, queryParams);
        HttpRequest req = buildRequest('GET', url, null, null);
        return executeWithRetry(req, namedCredential, endpoint);
    }

    /**
     * @description Performs an authenticated POST request with JSON body.
     * @param namedCredential  Named credential developer name
     * @param endpoint         Relative endpoint path
     * @param body             Request body object (serialized to JSON)
     * @return                 ApiResponse wrapper
     */
    public static ApiResponse doPost(String namedCredential, String endpoint, Object body) {
        String url         = buildUrl(namedCredential, endpoint, null);
        String jsonBody    = body != null ? JSON.serialize(body) : '{}';
        HttpRequest req    = buildRequest('POST', url, jsonBody,
            new Map<String, String>{'Content-Type' => 'application/json'});
        return executeWithRetry(req, namedCredential, endpoint);
    }

    /**
     * @description Performs an authenticated PUT request.
     * @param namedCredential  Named credential developer name
     * @param endpoint         Relative endpoint path
     * @param body             Request body object
     * @return                 ApiResponse wrapper
     */
    public static ApiResponse doPut(String namedCredential, String endpoint, Object body) {
        String url      = buildUrl(namedCredential, endpoint, null);
        String jsonBody = body != null ? JSON.serialize(body) : '{}';
        HttpRequest req = buildRequest('PUT', url, jsonBody,
            new Map<String, String>{'Content-Type' => 'application/json'});
        return executeWithRetry(req, namedCredential, endpoint);
    }

    /**
     * @description Performs a PATCH request for partial updates.
     */
    public static ApiResponse doPatch(String namedCredential, String endpoint, Object body) {
        String url      = buildUrl(namedCredential, endpoint, null);
        String jsonBody = body != null ? JSON.serialize(body) : '{}';
        HttpRequest req = buildRequest('PATCH', url, jsonBody,
            new Map<String, String>{'Content-Type' => 'application/json'});
        return executeWithRetry(req, namedCredential, endpoint);
    }

    /**
     * @description Performs a DELETE request.
     */
    public static ApiResponse doDelete(String namedCredential, String endpoint) {
        String url      = buildUrl(namedCredential, endpoint, null);
        HttpRequest req = buildRequest('DELETE', url, null, null);
        return executeWithRetry(req, namedCredential, endpoint);
    }

    // =========================================================================
    // LEGACY SYSTEM SPECIFIC METHODS
    // =========================================================================

    /**
     * @description Fetches legacy Account records by page from the legacy REST API.
     * @param page      Zero-based page number
     * @param pageSize  Number of records per page
     * @return          Deserialized list of legacy account maps
     */
    public static List<Map<String, Object>> fetchLegacyAccounts(Integer page, Integer pageSize) {
        Map<String, String> params = new Map<String, String>{
            'page'     => String.valueOf(page),
            'pageSize' => String.valueOf(pageSize),
            'status'   => 'ACTIVE'
        };
        ApiResponse resp = doGet(NC_LEGACY_SYSTEM, '/api/v2/accounts', params);
        if (!resp.isSuccess) {
            throw new IntegrationException(
                'Failed to fetch legacy accounts: ' + resp.errorMessage);
        }
        Map<String, Object> parsed = (Map<String, Object>) JSON.deserializeUntyped(resp.body);
        return (List<Map<String, Object>>) parsed.get('records');
    }

    /**
     * @description Validates an address using the Address Validation API.
     * @param street   Street address
     * @param city     City
     * @param state    State/Province
     * @param zip      ZIP / postal code
     * @param country  Country code
     * @return         Validated and standardized address map
     */
    public static Map<String, String> validateAddress(String street, String city,
                                                       String state, String zip,
                                                       String country) {
        Map<String, Object> body = new Map<String, Object>{
            'street'  => street,
            'city'    => city,
            'state'   => state,
            'zip'     => zip,
            'country' => country
        };
        ApiResponse resp = doPost(NC_VALIDATION_SVC, '/v1/validate', body);
        if (!resp.isSuccess) {
            AuditLogService.logError(LOG_SOURCE, 'validateAddress',
                'Address validation failed: ' + resp.errorMessage, null);
            // Return original values if validation service unavailable
            return new Map<String, String>{
                'street'=>street, 'city'=>city, 'state'=>state,
                'zip'=>zip, 'country'=>country, 'validated'=>'false'
            };
        }
        Map<String, Object> parsed = (Map<String, Object>) JSON.deserializeUntyped(resp.body);
        return new Map<String, String>{
            'street'    => (String) parsed.get('standardizedStreet'),
            'city'      => (String) parsed.get('standardizedCity'),
            'state'     => (String) parsed.get('stateCode'),
            'zip'       => (String) parsed.get('zip5'),
            'country'   => (String) parsed.get('countryCode'),
            'validated' => 'true',
            'dpv'       => (String) parsed.get('dpvConfirmation')
        };
    }

    // =========================================================================
    // PRIVATE HELPERS
    // =========================================================================

    /**
     * @description Executes an HttpRequest with exponential backoff retry logic.
     * @param req              Configured HttpRequest
     * @param namedCredential  For logging
     * @param endpoint         For logging
     * @return                 ApiResponse
     */
    private static ApiResponse executeWithRetry(HttpRequest req,
                                                 String namedCredential,
                                                 String endpoint) {
        Integer  attempt   = 0;
        Integer  delay     = BASE_RETRY_DELAY_MS;
        Exception lastEx;

        while (attempt <= MAX_RETRIES) {
            try {
                Long startMs = System.currentTimeMillis();
                HttpResponse resp = new Http().send(req);
                Long durationMs  = System.currentTimeMillis() - startMs;

                AuditLogService.logSuccess(LOG_SOURCE, 'HTTP_' + req.getMethod(),
                    namedCredential + endpoint
                    + ' | Status=' + resp.getStatusCode()
                    + ' | Duration=' + durationMs + 'ms'
                    + ' | Attempt=' + (attempt + 1), null);

                if (isRetryableStatus(resp.getStatusCode()) && attempt < MAX_RETRIES) {
                    attempt++;
                    delay *= 2; // exponential backoff
                    continue;
                }

                return buildApiResponse(resp);

            } catch (CalloutException ce) {
                lastEx = ce;
                AuditLogService.logError(LOG_SOURCE, 'HTTP_' + req.getMethod(),
                    'Attempt ' + (attempt + 1) + ' failed: ' + ce.getMessage(), null);

                if (attempt >= MAX_RETRIES) { break; }
                attempt++;
                delay *= 2;
            }
        }

        return ApiResponse.error(
            'All ' + (MAX_RETRIES + 1) + ' attempts failed. Last error: '
            + (lastEx != null ? lastEx.getMessage() : 'Unknown'));
    }

    private static HttpRequest buildRequest(String method, String url,
                                             String body, Map<String, String> headers) {
        HttpRequest req = new HttpRequest();
        req.setMethod(method);
        req.setEndpoint(url);
        req.setTimeout(DEFAULT_TIMEOUT_MS);
        req.setHeader('Accept',       'application/json');
        req.setHeader('X-Request-Id', generateRequestId());
        req.setHeader('X-Source-Org', UserInfo.getOrganizationId());

        if (headers != null) {
            for (String key : headers.keySet()) {
                req.setHeader(key, headers.get(key));
            }
        }
        if (String.isNotBlank(body)) {
            req.setBody(body);
        }
        return req;
    }

    private static String buildUrl(String namedCredential, String endpoint,
                                    Map<String, String> queryParams) {
        String base = NAMED_CREDENTIAL_PREFIX + namedCredential + endpoint;
        if (queryParams == null || queryParams.isEmpty()) { return base; }

        List<String> pairs = new List<String>();
        for (String key : queryParams.keySet()) {
            pairs.add(EncodingUtil.urlEncode(key, 'UTF-8')
                + '=' + EncodingUtil.urlEncode(queryParams.get(key), 'UTF-8'));
        }
        return base + '?' + String.join(pairs, '&');
    }

    private static ApiResponse buildApiResponse(HttpResponse resp) {
        ApiResponse ar  = new ApiResponse();
        ar.statusCode   = resp.getStatusCode();
        ar.body         = resp.getBody();
        ar.isSuccess    = resp.getStatusCode() >= 200 && resp.getStatusCode() < 300;
        ar.headers      = new Map<String, String>{
            'Content-Type' => resp.getHeader('Content-Type'),
            'X-Request-Id' => resp.getHeader('X-Request-Id')
        };
        if (!ar.isSuccess) {
            ar.errorMessage = 'HTTP ' + resp.getStatusCode() + ': ' + resp.getStatus()
                + ' | Body: ' + (resp.getBody() != null
                    ? resp.getBody().left(500) : '');
        }
        return ar;
    }

    private static Boolean isRetryableStatus(Integer status) {
        return status == 429 || status == 502 || status == 503 || status == 504;
    }

    private static String generateRequestId() {
        return EncodingUtil.convertToHex(
            Crypto.generateAesKey(128)).substring(0, 16).toUpperCase();
    }

    // =========================================================================
    // RESPONSE WRAPPER
    // =========================================================================

    public class ApiResponse {
        public Integer             statusCode;
        public String              body;
        public Boolean             isSuccess;
        public String              errorMessage;
        public Map<String, String> headers;

        public ApiResponse() {
            this.isSuccess = false;
            this.headers   = new Map<String, String>();
        }

        public static ApiResponse error(String message) {
            ApiResponse r  = new ApiResponse();
            r.isSuccess    = false;
            r.errorMessage = message;
            r.statusCode   = 0;
            return r;
        }

        /**
         * @description Deserializes the JSON body into an untyped map.
         * @return  Map<String, Object> or null if body is blank/invalid
         */
        public Map<String, Object> parseJson() {
            if (String.isBlank(body)) { return null; }
            try {
                return (Map<String, Object>) JSON.deserializeUntyped(body);
            } catch (Exception e) {
                return null;
            }
        }

        /**
         * @description Deserializes the JSON body into a typed list of maps.
         */
        public List<Object> parseJsonArray() {
            if (String.isBlank(body)) { return null; }
            try {
                return (List<Object>) JSON.deserializeUntyped(body);
            } catch (Exception e) {
                return null;
            }
        }
    }
}
