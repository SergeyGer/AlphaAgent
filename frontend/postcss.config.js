export default {
  plugins: {
    // Tailwind 4 moved its PostCSS plugin into a separate package. The
    // `tailwindcss` entry that worked in v3 is no longer a PostCSS plugin.
    '@tailwindcss/postcss': {},
    // autoprefixer is intentionally absent: Tailwind 4 targets the browsers
    // from its own config and applies the prefixes itself.
  },
};
