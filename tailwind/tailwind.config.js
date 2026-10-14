/** Tailwind 3.4.17 build for the admin page. Run through scripts/build_css.py. */
module.exports = {
  content: ["./src/templates/*.html"],
  // Light or dark follows the system setting; there is no toggle. The theme swap is
  // done by the CSS variables in input.css, so the markup carries no dark: variants.
  darkMode: "media",
  theme: {
    extend: {
      colors: {
        ink: "var(--ink)",
        paper: "var(--paper)",
        canvas: "var(--canvas)",
        "pop-red": "var(--red)",
        "pop-yellow": "var(--yellow)",
        "pop-violet": "var(--violet)",
      },
      fontFamily: {
        sans: ['"Space Grotesk"', "ui-sans-serif", "system-ui", "sans-serif"],
      },
    },
  },
  plugins: [],
};
