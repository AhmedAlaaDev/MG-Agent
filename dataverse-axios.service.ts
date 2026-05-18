import axios, {
  AxiosError,
  AxiosHeaders,
  AxiosInstance,
  InternalAxiosRequestConfig,
} from 'axios';

import { EnvService } from '@/modules/dataverse/services/dataverse-env.service';
import { DataverseTokenService } from '@/modules/dataverse/services/dataverse-token.service';

/**
 * Configuration options for retry behavior
 */
interface RetryConfig {
  maxRetries?: number;
  delay?: number;
  useExponentialBackoff?: boolean;
}

/**
 * Extended Axios request config with retry metadata
 */
interface RetryAxiosRequestConfig extends InternalAxiosRequestConfig {
  _retryCount?: number;
  _retryConfig?: RetryConfig;
}

/**
 * A service that provides a configured Axios instance for making HTTP requests.
 *
 * This service creates an Axios instance with appropriate headers for OData requests
 * and can optionally inject authentication tokens automatically. It's designed to
 * simplify API communication with Dynamics 365.
 *
 * This is a singleton service - use getInstance() to get the shared instance.
 */
export class DataverseAxiosService {
  private static _instance: DataverseAxiosService | null = null;

  private _axiosInstance: AxiosInstance;

  public baseUrl: string;

  private readonly retryConfig: RetryConfig;

  /**
   * Private constructor to enforce singleton pattern.
   * Use getInstance() to get the shared instance.
   */
  private constructor(retryConfig: RetryConfig = {}) {
    const appApiUrl = EnvService.getInstance().get('AZURE_APP_API_URL');

    if (!appApiUrl) {
      throw new Error('Azure App API URL is not configured');
    }

    this.baseUrl = appApiUrl;
    this.retryConfig = {
      maxRetries: retryConfig.maxRetries ?? 5,
      delay: retryConfig.delay ?? 2000,
      useExponentialBackoff: retryConfig.useExponentialBackoff ?? false,
    };

    this._axiosInstance = axios.create({
      baseURL: appApiUrl,
    });

    this._injectConfig();
    this._injectToken();
    this._injectRetry();
  }

  /**
   * Gets the singleton instance of DataverseAxiosService.
   * @param retryConfig - Optional retry configuration (only used on first initialization)
   * @returns The singleton instance
   */
  public static getInstance(
    retryConfig: RetryConfig = {},
  ): DataverseAxiosService {
    if (!DataverseAxiosService._instance) {
      DataverseAxiosService._instance = new DataverseAxiosService(retryConfig);
    }
    return DataverseAxiosService._instance;
  }

  /**
   * Returns the configured Axios instance
   */
  public get instance(): AxiosInstance {
    return this._axiosInstance;
  }

  /**
   * Injects default configuration and headers into all requests
   */
  private _injectConfig(): void {
    this._axiosInstance.interceptors.request.use((config) => {
      const customHeaders = {
        'OData-MaxVersion': '4.0',
        'OData-Version': '4.0',
        Accept: 'application/json',
        Prefer: 'odata.include-annotations="*"',
      };

      config.headers = AxiosHeaders.from({
        ...customHeaders,
        ...(config.headers || {}),
      });

      return config;
    });
  }

  /**
   * Injects authentication token into all requests
   * @throws Error if token cannot be retrieved
   */
  private _injectToken(): void {
    this._axiosInstance.interceptors.request.use(async (config) => {
      const { accessToken } =
        await DataverseTokenService.getInstance().getAccessToken();

      // Now, handle the token injection
      if (!accessToken) {
        // This error will be thrown for both dev and prod if the token is missing
        throw new Error('Failed to inject token, Token is missing!!');
      }

      config.headers.Authorization = `Bearer ${accessToken}`;
      return config;
    });
  }

  /**
   * Injects retry logic into all requests
   * Retries failed requests up to maxRetries times with configurable delay
   * Only retries on network errors, connection errors, and 5xx server errors
   * Does not retry on client errors (4xx) like 401, 403, 404, etc.
   */
  private _injectRetry(): void {
    this._axiosInstance.interceptors.response.use(
      (response) => response,
      async (error: AxiosError) => {
        const config = error.config as RetryAxiosRequestConfig;

        // Don't retry if config is not available
        if (!config) {
          return Promise.reject(error);
        }

        // Check if this is a retryable error
        if (!this._shouldRetry(error)) {
          return Promise.reject(error);
        }

        // Initialize retry count if not set
        config._retryCount = config._retryCount ?? 0;
        config._retryConfig = config._retryConfig ?? this.retryConfig;

        const { maxRetries, delay, useExponentialBackoff } =
          config._retryConfig;

        // Ensure retry config values are defined (should always be set in constructor)
        const maxRetriesValue = maxRetries ?? 3;
        const delayValue = delay ?? 1000;

        // Check if we should retry
        if (config._retryCount >= maxRetriesValue) {
          console.info(
            `[Axios Retry] Max retries (${maxRetriesValue}) reached for ${config.method?.toUpperCase()} ${config.url}`,
          );
          return Promise.reject(error);
        }

        // Calculate delay (exponential backoff if enabled)
        const retryDelay = useExponentialBackoff
          ? delayValue * Math.pow(2, config._retryCount)
          : delayValue;

        config._retryCount += 1;

        console.info(
          `[Axios Retry] Retrying request (attempt ${config._retryCount}/${maxRetriesValue}) for ${config.method?.toUpperCase()} ${config.url} after ${retryDelay}ms`,
        );

        // Wait before retrying
        await new Promise((resolve) => setTimeout(resolve, retryDelay));

        // Retry the request
        return this._axiosInstance(config);
      },
    );
  }

  /**
   * Determines if an error should be retried
   * Retries on:
   * - Network/connection errors (no response)
   * - 5xx server errors (500, 502, 503, 504, etc.)
   * - 429 Too Many Requests
   * Does not retry on:
   * - 4xx client errors (400, 401, 403, 404, etc.)
   */
  private _shouldRetry(error: AxiosError): boolean {
    // Retry on network errors (no response received)
    if (!error.response) {
      return true;
    }

    const statusCode = error.response.status;

    // Retry on 5xx server errors
    if (statusCode >= 500 && statusCode < 600) {
      return true;
    }

    // Retry on 429 Too Many Requests (rate limiting)
    if (statusCode === 429) {
      return true;
    }

    // Don't retry on 4xx client errors (401, 403, 404, etc.)
    return false;
  }
}
