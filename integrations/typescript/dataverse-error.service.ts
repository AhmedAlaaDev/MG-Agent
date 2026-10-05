import { AxiosError } from 'axios';

/**
 * A singleton service for standardized error handling across the application.
 *
 * This class provides a consistent approach to error handling, particularly for
 * asynchronous operations and API calls. It transforms various error types into
 * standardized Error objects with meaningful messages.
 *
 * This is a singleton service - use getInstance() to get the shared instance.
 */
export class DataverseErrorService {
  private static _instance: DataverseErrorService | null = null;

  /**
   * Private constructor to enforce singleton pattern.
   * Use getInstance() to get the shared instance.
   */
  private constructor() {}

  /**
   * Gets the singleton instance of DataverseErrorService.
   * @returns The singleton instance
   */
  public static getInstance(): DataverseErrorService {
    if (!DataverseErrorService._instance) {
      DataverseErrorService._instance = new DataverseErrorService();
    }
    return DataverseErrorService._instance;
  }

  /**
   * Executes an asynchronous function and handles any errors that occur.
   *
   * @template T - The return type of the provided function
   * @param fn - The asynchronous function to execute
   * @returns A promise that resolves to the result of the function or rejects with a standardized Error
   *
   * @example
   * ```typescript
   * const errorService = DataverseErrorService.getInstance();
   * const data = await errorService.handle(async () => {
   *   return await apiClient.getData();
   * });
   * ```
   */
  public async handle<T>(fn: () => Promise<T>): Promise<T> {
    try {
      return await fn();
    } catch (error) {
      if (process.env.NODE_ENV === 'development') {
        console.error('🔴 Error:', error);
      }
      throw this._handleError(error);
    }
  }

  /**
   * Processes an unknown error into a standardized Error object.
   *
   * This method handles various error types including:
   * - Native Error objects
   * - Axios errors (with special handling for response and request errors)
   * - String errors
   * - Object errors with message properties
   *
   * @param error - The unknown error to process
   * @returns A standardized Error object with a meaningful message
   * @private
   */
  private _handleError(error: unknown): Error {
    // First, handle common network errors with user-friendly messages
    // Extract error code from various possible locations in the error object
    const netCode =
      (error as any)?.code ||
      (error as any)?.cause?.code ||
      (error instanceof AxiosError ? error.code : undefined);

    // Connection was reset by the server (common with load balancers/proxies)
    if (netCode === 'ECONNRESET') {
      return new Error(
        '🔌 Network error: connection was reset by the server. Please retry.',
      );
    }
    // Request timed out (server took too long to respond)
    if (netCode === 'ETIMEDOUT') {
      return new Error(
        '⏳ Network timeout while contacting the server. Please retry.',
      );
    }
    // Request was aborted (usually due to timeout or cancellation)
    if (netCode === 'ECONNABORTED') {
      return new Error('🛑 Request aborted (likely timeout). Please retry.');
    }

    // Handle Axios HTTP errors with proper status code handling
    if (error instanceof AxiosError) {
      // Special handling for authentication errors
      if (error.status === 401) {
        throw new Error('Unauthorized. No token provided or invalid token.');
      }

      // Extract error details from response body
      const errorData = error.response?.data;

      // Try to get structured error message (common in REST APIs and OData)
      if (errorData?.error?.message) {
        return new Error(errorData.error.message as string);
      }

      // Handle cases where error data is a plain string (e.g. batch multipart, HTML)
      if (errorData && typeof errorData === 'string') {
        return new Error(this._extractMessageFromText(errorData));
      }

      // Handle object that might be batch/parsed response with nested error
      if (errorData && typeof errorData === 'object') {
        const fromText = this._extractMessageFromText(
          JSON.stringify(errorData),
        );
        if (!fromText.includes('Unknown error message format')) {
          return new Error(fromText);
        }
      }
    }

    // If it's already a proper Error object, return as-is
    if (error instanceof Error) {
      return error;
    }

    // Handle string errors (thrown as strings rather than Error objects)
    if (typeof error === 'string') {
      return new Error(this._extractMessageFromText(error));
    }

    // Handle error-like objects with message property (common in some libraries)
    if (
      typeof error === 'object' &&
      error !== null &&
      'message' in error &&
      typeof error.message === 'string'
    ) {
      return new Error(error?.message);
    }

    // Last resort: unknown error format
    return new Error('❌ Unknown error occurred.');
  }

  /**
   * Extracts meaningful error messages from text strings.
   *
   * Attempts to parse error messages from various formats including:
   * - Dynamics batch error responses (multipart with embedded JSON)
   * - OData/Dataverse error format { "error": { "message": "..." } }
   * - JSON error objects
   * - Plain text errors
   *
   * @param text - The error text to process
   * @returns A formatted error message
   * @private
   */
  private _extractMessageFromText(text: string): string {
    // OData/Dataverse and batch: "message":"..." or "Message":"..." (allow escaped quotes)
    const messageDouble = text.match(
      /"([Mm]essage)"\s*:\s*"((?:[^"\\]|\\.)*)"/,
    );
    if (messageDouble?.[2]) {
      return `❌ ${this._unescapeJsonString(messageDouble[2])}`;
    }

    // Single-quoted message (some APIs)
    const messageSingle = text.match(/'message'\s*:\s*'((?:[^'\\]|\\.)*)'/);
    if (messageSingle?.[1]) {
      return `❌ ${this._unescapeJsonString(messageSingle[1])}`;
    }

    // Try to parse as JSON (full body or a part of multipart)
    try {
      const parsed = JSON.parse(text);
      if (parsed?.error?.message) {
        return `❌ ${parsed.error.message}`;
      }
    } catch {
      // Not valid JSON; try to find a JSON error object embedded in multipart/batch
      const errorBlock = text.match(/\{\s*"error"\s*:\s*\{[^}]+\}\s*\}/);
      if (errorBlock?.[0]) {
        try {
          const inner = JSON.parse(errorBlock[0]);
          if (inner?.error?.message) {
            return `❌ ${inner.error.message}`;
          }
        } catch {
          // ignore
        }
      }
    }

    return '❌ Unknown error message format.';
  }

  private _unescapeJsonString(s: string): string {
    return s
      .replace(/\\"/g, '"')
      .replace(/\\\\/g, '\\')
      .replace(/\\n/g, '\n')
      .replace(/\\t/g, '\t');
  }
}
