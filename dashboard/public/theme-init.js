// Applies the saved theme before first paint so dark mode doesn't flash white.
// A plain file (not inline) so a strict Content-Security-Policy can allow it as 'self'.
// Light is the default; dark only when the owner picked it.
try {
  document.documentElement.classList.toggle("dark", localStorage.getItem("resonant.theme") === "dark");
} catch (e) {
  /* storage blocked: light */
}
