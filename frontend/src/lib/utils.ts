import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** Display labels for the morphometry service's canonical anatomy/projection keys.
 *  Display only — the keys themselves are what travel to the server, so an unknown key
 *  (a mode the service gained since) falls through and shows verbatim rather than
 *  disappearing. Used by the capability list AND the chips, so both read identically.
 *
 *  Regions get their German names; PROJECTIONS keep the radiological term — "lateral" and
 *  "axial", not "seitlich" — because that is the vocabulary of the reports and the reader.
 *  Both fall through untranslated, so they are absent here by design, not by omission. */
const DE: Record<string, string> = {
  knee: "Knie", feet: "Fuß", shoulder: "Schulter", hip: "Hüfte", ankle: "Sprunggelenk",
  ap: "a.p.", pa: "p.a.",
};

export const de = (s: string) => DE[s] ?? s;
