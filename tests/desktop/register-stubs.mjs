import { registerHooks, register } from 'node:module'
import { resolve } from './stub-loader.mjs'

if (typeof registerHooks === 'function') {
  registerHooks({ resolve })
} else {
  register('./stub-loader.mjs', import.meta.url)
}
