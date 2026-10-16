import type { ReactNode } from "react";

/** Optional UI extensions.
 *
 *  A module in src/plugins/ whose default export is a `UseSessionPlugin` hook is picked up
 *  at build time. It can record interaction events, replace the whole view (e.g. with a
 *  worklist), add a bar under the header and lock parts of the UI. Without a plugin the
 *  app runs as is and every recorder call is a no-op.
 *
 *  Backend counterpart: plugins/ (see plugins/__init__.py). */

/** Receives interaction events. Inert unless a plugin activates it. */
export interface Recorder {
  active: boolean;
  /** Sent with /api/analyze_stream so the server can attach its own events to the same
   *  recorded session. */
  sessionUid: string | null;
  log: (event: string, payload?: Record<string, unknown>) => void;
  /** Attach a running agent session to the recorded session. */
  bind: (sessionId: string) => Promise<void>;
}

/** What a plugin can see of, and do to, the case on screen. Read through a ref, so the
 *  values are current whenever the plugin uses them (in an effect or a click handler). */
export interface CaseHost {
  sessionId: string | null;
  reportMd: string;
  /** Whether an analysis was started on this case. */
  started: boolean;
  pickImage: (file: File) => void;
  /** Clear the case: report, chat, session and landmarks. */
  clearCase: () => void;
}

export interface PluginSettings {
  profile: string;
  thinking: boolean;
  numReferences: number;
}

export interface SessionPlugin {
  recorder: Recorder;
  /** A full-screen view that replaces the assistant, or null for the normal UI. */
  takeover: ReactNode;
  /** Rendered under the header. */
  banner: ReactNode;
  /** The plugin supplies the image; hide the file picker. */
  lockImage: boolean;
  /** Keep the region/projection chips locked until the model has assessed the image. */
  lockAssessmentUntilAssessed: boolean;
}

export type UseSessionPlugin = (
  settings: PluginSettings,
  host: { readonly current: CaseHost }
) => SessionPlugin;

const INERT: SessionPlugin = {
  recorder: { active: false, sessionUid: null, log: () => {}, bind: async () => {} },
  takeover: null,
  banner: null,
  lockImage: false,
  lockAssessmentUntilAssessed: false,
};

const useNoPlugin: UseSessionPlugin = () => INERT;

// Resolved at build time; without a src/plugins/ folder this is simply empty. One plugin
// at most: two hooks that each want to replace the view cannot be combined safely.
const found = Object.values(
  import.meta.glob<{ default: UseSessionPlugin }>("../plugins/*.{ts,tsx}", { eager: true })
);

export const useSessionPlugin: UseSessionPlugin = found[0]?.default ?? useNoPlugin;
