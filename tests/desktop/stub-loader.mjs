// Resolve hooks mapping the three SDK bare specifiers to local stubs.
// Synchronous on purpose: registerHooks() only accepts sync hooks.
export function resolve(specifier, context, nextResolve) {
  if (specifier === '@hermes/plugin-sdk') {
    return { url: new URL('./stubs/plugin-sdk.mjs', import.meta.url).href, shortCircuit: true }
  }
  if (specifier === 'react/jsx-runtime' || specifier === 'react/jsx-dev-runtime') {
    return { url: new URL('./stubs/jsx-runtime.mjs', import.meta.url).href, shortCircuit: true }
  }
  if (specifier === 'react') {
    return { url: new URL('./stubs/react.mjs', import.meta.url).href, shortCircuit: true }
  }
  return nextResolve(specifier, context)
}
