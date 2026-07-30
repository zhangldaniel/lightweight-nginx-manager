/// <reference types="vite/client" />

declare module 'vanta/dist/vanta.waves.min' {
  const waves: (options: Record<string, unknown>) => { destroy: () => void }
  export default waves
}
