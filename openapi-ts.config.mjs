export default {
  input: 'waldur-openapi-schema.yaml',
  output: 'waldur-typescript-sdk',
  plugins: [
    "@hey-api/sdk",
    "@hey-api/client-fetch",
    "@hey-api/typescript",
  ],
  parser: {
    transforms: {
      // Keep a single model per schema (no Readable/Writable split) so the
      // generated type surface matches what the frontend already imports.
      // Replaces the pre-0.78 `readOnlyWriteOnlyBehavior: "off"` option.
      readWrite: {
        enabled: false,
      },
    },
  },
};
