import fs from 'fs';
import path from 'path';

const sdkDir = path.resolve('waldur-typescript-sdk');

// 1. Drop querySerializer from sdk.gen.ts
const sdkGenPath = path.join(sdkDir, 'sdk.gen.ts');
if (fs.existsSync(sdkGenPath)) {
  let sdkGenCode = fs.readFileSync(sdkGenPath, 'utf-8');

  let lines = sdkGenCode.split('\n');
  let inQuerySerializer = false;
  let outLines = [];
  for (let line of lines) {
    if (line.startsWith('    querySerializer: {')) {
      inQuerySerializer = true;
      continue;
    }
    if (inQuerySerializer) {
      if (/^    (\w+:|\.\.\.options)/.test(line) || line.startsWith('});')) {
        inQuerySerializer = false;
        outLines.push(line);
      }
      continue;
    }
    outLines.push(line);
  }
  sdkGenCode = outLines.join('\n');

  // Deduplicate bearer token entries in security arrays
  sdkGenCode = sdkGenCode.replace(/ {8}\{ scheme: 'bearer', type: 'http' \},\n {8}\{ scheme: 'bearer', type: 'http' \}/g, "        { scheme: 'bearer', type: 'http' }");

  fs.writeFileSync(sdkGenPath, sdkGenCode);
} else {
  console.error('sdk.gen.ts not found');
  process.exit(1);
}

// 2. Drop undefined data error block from RequestResult in client/types.gen.ts
const typesGenPath = path.join(sdkDir, 'client', 'types.gen.ts');
if (fs.existsSync(typesGenPath)) {
  let typesCode = fs.readFileSync(typesGenPath, 'utf-8');
  typesCode = typesCode.replace(
    /\s*\|\s*\{\s*data:\s*undefined;\s*error:\s*TError extends Record<string, unknown>\s*\?\s*TError\[keyof TError\]\s*:\s*TError;\s*\}/g,
    ''
  );
  fs.writeFileSync(typesGenPath, typesCode);
} else {
  console.warn('client/types.gen.ts not found');
}

// 3. Create sparse-types.ts
const sparseTypesCode = `import type { Options } from './client';
import type { RequestResult } from './client';

export type ExtractData<T> = T extends Record<string, unknown> ? T[keyof T] : T;
export type ExtractModel<T> = T extends Array<infer U> ? U : T;

// The universal wrapper that enforces ThrowOnError = true globally
type MapArgs<TArgs extends any[]> = {
    [K in keyof TArgs]: K extends '0' ? (
        Omit<NonNullable<TArgs[0]>, 'throwOnError'> & { throwOnError?: true } | Extract<TArgs[0], undefined>
    ) : TArgs[K]
};

type MapSparseArgs<TArgs extends any[], TData, TField> = {
    [K in keyof TArgs]: K extends '0' ? (
        Omit<NonNullable<TArgs[0]>, 'query' | 'throwOnError'> & {
            query?: Omit<TData extends { query?: infer Q } ? NonNullable<Q> : {}, 'field'> & { field?: TField },
            throwOnError?: true
        } | Extract<TArgs[0], undefined>
    ) : TArgs[K]
};

export type DynamicHeyApiFunc<TData, TRes, TErr, TFunc extends (...args: any) => any> =
    TData extends { query?: infer Q }
        ? ('field' extends keyof NonNullable<Q>
            ? <const TField extends ReadonlyArray<keyof ExtractModel<ExtractData<TRes>>> | never = never>(
                ...args: MapSparseArgs<Parameters<TFunc>, TData, TField>
            ) => Promise<
                [TField] extends [never]
                    ? Awaited<RequestResult<TRes, TErr, true, "fields">>
                    : Omit<Awaited<RequestResult<TRes, TErr, true, "fields">>, 'data'> & {
                        data: ExtractData<TRes> extends Array<any>
                            ? Array<Pick<ExtractModel<ExtractData<TRes>>, Extract<TField[number], keyof ExtractModel<ExtractData<TRes>>>>>
                            : Pick<ExtractModel<ExtractData<TRes>>, Extract<TField[number], keyof ExtractModel<ExtractData<TRes>>>>
                      }
            >
            : (...args: MapArgs<Parameters<TFunc>>) => Promise<Awaited<RequestResult<TRes, TErr, true, "fields">>>)
        : (...args: MapArgs<Parameters<TFunc>>) => Promise<Awaited<RequestResult<TRes, TErr, true, "fields">>>;
`;
fs.writeFileSync(path.join(sdkDir, 'sparse-types.ts'), sparseTypesCode);

// 4. Update index.ts to export strictly typed functions and patch sdk.gen.ts proxies
if (fs.existsSync(sdkGenPath)) {
  const code = fs.readFileSync(sdkGenPath, 'utf-8');
  // Regex to match: export const functionName = <ThrowOnError...>(options?: Options<TData, ThrowOnError>) => (options?.client ?? client).method<TRes, TErr, ThrowOnError>({...})
  const regex = /export const (\w+)\s*=\s*<[^>]+>\(options\??:\s*Options<([^,]+),\s*[^>]+>\)\s*=>\s*\(options\??\.client \?\? client\)\.\w+<([^,]+),\s*([^,]+),\s*[^>]+>\(\{/g;

  let out = `// AUTO-GENERATED: DO NOT EDIT\n`;
  out += `export { formDataBodySerializer, RequestResult } from "./client";\n`;
  out += `export * from './types.gen';\n`;
  out += `import type { DynamicHeyApiFunc } from './sparse-types';\n`;

  const matches = [...code.matchAll(regex)];

  // Import all original functions from sdk.gen
  const fnNames = matches.map(m => m[1]);
  if(fnNames.length > 0) {
    out += `import { ${fnNames.join(', ')} } from './sdk.gen';\n\n`;
  }

  // Collect and import all required types from types.gen
  const typeNames = new Set();
  for (const match of matches) {
    const [_, fnName, tData, tRes, tErr] = match;
    [tData, tRes, tErr].forEach(t => {
      t = t.trim();
      if (!['unknown', 'void', 'Blob', 'string', 'number', 'boolean', 'any', 'File'].includes(t)) {
        typeNames.add(t);
      }
    });
  }
  if (typeNames.size > 0) {
    out += `import type { ${Array.from(typeNames).join(', ')} } from './types.gen';\n\n`;
  }

  // Generate the strictly typed re-exports
  for (const match of matches) {
    const [_, fnName, tData, tRes, tErr] = match;
    out += `const _${fnName} = ${fnName} as unknown as DynamicHeyApiFunc<${tData}, ${tRes}, ${tErr}, typeof ${fnName}>;\n`;
    out += `export { _${fnName} as ${fnName} };\n`;
  }

  fs.writeFileSync(path.join(sdkDir, 'index.ts'), out);
  console.log('✅ SDK Patched! ThrowOnError preserved, perfect Tree-Shaking intact.');
}
