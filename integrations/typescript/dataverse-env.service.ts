import dotenv from 'dotenv';

// Load environment-specific .env file based on NODE_ENV
const envFile =
  process.env.NODE_ENV === 'production'
    ? '.env.production'
    : '.env.development';
dotenv.config({ path: envFile, quiet: true });

/**
 * Singleton service for accessing environment variables with type safety
 *
 * This service provides a type-safe way to access environment variables from .env.local files.
 * It supports automatic type conversion for string, number, and boolean values.
 *
 * The service centralizes environment variable access throughout the application,
 * ensuring consistent access patterns and proper error handling.
 *
 * This is a singleton service - use getInstance() to get the shared instance.
 *
 * @example
 * const envService = EnvService.getInstance();
 * const apiUrl = envService.get<string>('API_URL');
 * const port = envService.get<number>('PORT');
 * const isProduction = envService.get<boolean>('IS_PRODUCTION');
 * const [apiUrl, apiKey] = envService.getAll(['API_URL', 'API_KEY']);
 */
export class EnvService {
  private static _instance: EnvService | null = null;

  /**
   * Private constructor to enforce singleton pattern.
   * Use getInstance() to get the shared instance.
   */
  private constructor() {}

  /**
   * Gets the singleton instance of EnvService.
   * @returns The singleton instance
   */
  public static getInstance(): EnvService {
    if (!EnvService._instance) {
      EnvService._instance = new EnvService();
    }
    return EnvService._instance;
  }

  /**
   * Gets an environment variable with type conversion
   *
   * @template T - The expected type of the environment variable (string, number, or boolean)
   * @param {string} key - The name of the environment variable to retrieve
   * @returns {T} - The environment variable value converted to the specified type
   * @throws {Error} - If the environment variable is not defined or cannot be converted to the specified type
   *
   * @example
   * // Get a string value (default)
   * const envService = EnvService.getInstance();
   * const apiKey = envService.get('API_KEY');
   *
   * // Get a number value
   * const port = envService.get<number>('PORT');
   *
   * // Get a boolean value
   * const debug = envService.get<boolean>('DEBUG_MODE');
   *
   */
  public get<T extends string | number | boolean = string>(key: string): T {
    const raw = process.env?.[key];

    if (raw === undefined || raw === '') {
      throw new Error(
        `❌ Missing environment variable "${key}".\n➡️ Please ensure it is defined in your .env file at the project root.`,
      );
    }

    // 🔄 Try to cast based on the generic type
    const targetType = typeof ({} as T);

    if (targetType === 'number') {
      const parsed = Number(raw);
      if (isNaN(parsed)) {
        throw new Error(`❌ "${key}" must be a valid number. Found: "${raw}"`);
      }
      return parsed as T;
    }

    if (targetType === 'boolean') {
      if (raw === 'true' || raw === '1') return true as T;
      if (raw === 'false' || raw === '0') return false as T;

      throw new Error(
        `❌ Environment variable "${key}" must be "true", "false", "1", or "0". Found: "${raw}"`,
      );
    }

    return raw as T;
  }

  /**
   * Gets multiple environment variables as strings
   *
   * @param {string[]} keys - Array of environment variable names to retrieve
   * @returns {string[]} - Array of environment variable values as strings
   * @throws {Error} - If any of the environment variables are not defined
   *
   * @example
   * // Get multiple environment variables
   * const envService = EnvService.getInstance();
   * const [apiUrl, apiKey] = envService.getAll(['API_URL', 'API_KEY']);
   */
  public getAll(keys: string[]): string[] {
    return keys.map((k) => this.get<string>(k));
  }

  /**
   * Gets an optional environment variable with type conversion and default value
   *
   * @template T - The expected type of the environment variable (string, number, or boolean)
   * @param {string} key - The name of the environment variable to retrieve
   * @param {T} defaultValue - Default value to return if the variable is not defined
   * @returns {T} - The environment variable value converted to the specified type, or the default value
   *
   * @example
   * // Get an optional number with default
   * const envService = EnvService.getInstance();
   * const timeout = envService.getOptional<number>('TIMEOUT', 5000);
   *
   * // Get an optional boolean with default
   * const enabled = envService.getOptional<boolean>('FEATURE_ENABLED', false);
   */
  public getOptional<T extends string | number | boolean = string>(
    key: string,
    defaultValue: T,
  ): T {
    const raw = process.env?.[key];

    if (raw === undefined || raw === '') {
      return defaultValue;
    }

    // 🔄 Try to cast based on the generic type
    const targetType = typeof defaultValue;

    if (targetType === 'number') {
      const parsed = Number(raw);
      if (isNaN(parsed)) {
        return defaultValue;
      }
      return parsed as T;
    }

    if (targetType === 'boolean') {
      if (raw === 'true' || raw === '1') return true as T;
      if (raw === 'false' || raw === '0') return false as T;
      return defaultValue;
    }

    return raw as T;
  }
}
