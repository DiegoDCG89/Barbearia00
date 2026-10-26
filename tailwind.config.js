// Tailwind v3: gera static/tailwind.css só com as classes usadas nos templates.
// Cores e fonte do tema "Dark Premium" do site (dourado + preto, Playfair Display nos títulos).
module.exports = {
  content: ["./templates/**/*.html"],
  theme: { extend: {
    colors: { gold: { DEFAULT: '#c19b6c', hover: '#a8865b' }, dark: { 900: '#0a0a0a', 800: '#111111', 700: '#1a1a1a', 600: '#2a2a2a' } },
    fontFamily: { serif: ['Playfair Display', 'serif'] }
  } },
  plugins: []
};
