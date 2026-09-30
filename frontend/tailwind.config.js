/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        terminal: {
          950: '#070b16',
          900: '#0b1120',
          850: '#0f172a',
          800: '#131c31',
          700: '#1e293b',
          600: '#334155',
        },
      },
      fontFamily: {
        sans: [
          'Inter',
          'ui-sans-serif',
          'system-ui',
          '-apple-system',
          'Segoe UI',
          'Roboto',
          'Helvetica Neue',
          'Arial',
          'sans-serif',
        ],
        mono: [
          'ui-monospace',
          'SFMono-Regular',
          'SF Mono',
          'Menlo',
          'Monaco',
          'Consolas',
          'Liberation Mono',
          'Courier New',
          'monospace',
        ],
      },
      boxShadow: {
        panel: '0 1px 2px rgba(2, 6, 23, 0.6), 0 8px 24px -12px rgba(2, 6, 23, 0.9)',
        glow: '0 0 0 1px rgba(16, 185, 129, 0.35), 0 0 18px -4px rgba(16, 185, 129, 0.45)',
      },
      keyframes: {
        'feed-in': {
          '0%': { opacity: '0', transform: 'translateY(-6px)', backgroundColor: 'rgba(16,185,129,0.10)' },
          '60%': { opacity: '1', backgroundColor: 'rgba(16,185,129,0.06)' },
          '100%': { opacity: '1', transform: 'translateY(0)', backgroundColor: 'transparent' },
        },
        'value-flash': {
          '0%': { backgroundColor: 'rgba(56,189,248,0.22)' },
          '100%': { backgroundColor: 'transparent' },
        },
        'flash-up': {
          '0%': { backgroundColor: 'rgba(16,185,129,0.22)' },
          '100%': { backgroundColor: 'transparent' },
        },
        'flash-down': {
          '0%': { backgroundColor: 'rgba(244,63,94,0.22)' },
          '100%': { backgroundColor: 'transparent' },
        },
        'live-pulse': {
          '0%, 100%': { opacity: '1', transform: 'scale(1)' },
          '50%': { opacity: '0.35', transform: 'scale(1.35)' },
        },
        'fade-in': {
          from: { opacity: '0' },
          to: { opacity: '1' },
        },
        shimmer: {
          '100%': { transform: 'translateX(100%)' },
        },
      },
      animation: {
        'feed-in': 'feed-in 520ms ease-out',
        'value-flash': 'value-flash 900ms ease-out',
        'flash-up': 'flash-up 900ms ease-out',
        'flash-down': 'flash-down 900ms ease-out',
        'live-pulse': 'live-pulse 1.6s ease-in-out infinite',
        'fade-in': 'fade-in 220ms ease-out',
        shimmer: 'shimmer 1.6s infinite',
      },
      transitionTimingFunction: {
        terminal: 'cubic-bezier(0.22, 1, 0.36, 1)',
      },
    },
  },
  plugins: [],
};
