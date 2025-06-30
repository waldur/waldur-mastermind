export default {
  input: 'waldur-openapi-schema.yaml',
  output: 'waldur-typescript-sdk',
  exportCore: true,
  plugins: [
    "@hey-api/sdk",
    "@hey-api/client-fetch",
    {
      name: "@hey-api/typescript",
      readOnlyWriteOnlyBehavior: "off",
    },
  ],
};
