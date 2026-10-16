// Applies the saved theme before first paint so dark mode doesn't flash white.
// A plain file (not inline) so a strict Content-Security-Policy can allow it as 'self'.
try {
  var t = localStorage.getItem("resonant.theme");
  var dark = t === "dark" || (t !== "light" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", dark);
} catch (e) {
  /* storage blocked: main.tsx applies the system theme */
}
