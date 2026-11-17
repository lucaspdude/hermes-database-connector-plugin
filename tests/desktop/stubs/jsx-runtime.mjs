// Minimal react/jsx-runtime shape: elements are plain inspectable trees.
export const Fragment = Symbol.for('react.fragment')
export const jsx = (type, props) => ({ type, props: props ?? {} })
export const jsxs = jsx
