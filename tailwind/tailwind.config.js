/** Tailwind 3.4.17 build for the admin page. Run through scripts/build_css.py. */
module.exports = {
  content: ["./src/templates/*.html"],
  theme: {
    extend: {
      colors: {
        indigo: {
          50: "#eff6ff",
          100: "#dbeafe",
          200: "#bfdbfe",
          300: "#93c5fd",
          400: "#60a5fa",
          500: "#0080ff",
          600: "#0057b8",
          700: "#00458f",
          800: "#073b70",
          900: "#0c3155",
          950: "#08223b",
        },
      },
    },
  },
  plugins: [],
};
