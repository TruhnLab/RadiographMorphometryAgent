/// <reference types="vite/client" />

// VITE_BUILD_SHA can be stamped at build time and recorded with a session, so a change of
// the frontend between recorded sessions stays detectable.
interface ImportMetaEnv {
  readonly VITE_BUILD_SHA?: string;
}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}
