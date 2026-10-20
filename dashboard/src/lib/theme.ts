import { useCallback, useEffect, useState } from "react";

export type Theme = "light" | "dark";
const KEY = "resonant.theme";

/** Light unless the owner picked dark (the OS setting is not followed). */
function readTheme(): Theme {
  try {
    return localStorage.getItem(KEY) === "dark" ? "dark" : "light";
  } catch {
    return "light";
  }
}

export function applyTheme(theme: Theme = readTheme()): void {
  document.documentElement.classList.toggle("dark", theme === "dark");
}

/** Toggles light ↔ dark and remembers the choice. */
export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(readTheme);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const toggle = useCallback(() => {
    setTheme((t) => {
      const next: Theme = t === "light" ? "dark" : "light";
      try {
        if (next === "light") localStorage.removeItem(KEY);
        else localStorage.setItem(KEY, next);
      } catch {
        // Storage can be blocked; the choice then lasts for this page only.
      }
      return next;
    });
  }, []);

  return [theme, toggle];
}
