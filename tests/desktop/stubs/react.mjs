// One-shot hook stubs: initializers run, setters are no-ops.
export const useState = (init) => [typeof init === 'function' ? init() : init, () => {}]
export const useMemo = (fn) => fn()
export const useCallback = (fn) => fn
export const useRef = (init) => ({ current: init })
