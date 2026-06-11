/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_AUTO_TRADE_API_KEY?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
