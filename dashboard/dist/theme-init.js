// Applies the saved theme before first paint so dark mode doesn't flash white.
// A plain file (not inline) so a strict Content-Security-Policy can allow it as 'self'.
// Light is the default; dark only when the owner picked it. Colours match lib/theme.ts.
try {
  if (localStorage.getItem("resonant.theme") === "dark") {
    document.documentElement.classList.add("dark");
    var meta = document.getElementById("theme-color");
    if (meta) meta.setAttribute("content", "#0a0a0a");
  }
} catch (e) {
  /* storage blocked: light */
}
