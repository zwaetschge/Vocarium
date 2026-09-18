import { useEffect, useRef } from 'react';
import * as THREE from 'three';
import { subscribeReactive } from '../lib/audioReactive';

/**
 * Three.js-Aurora: ein Fullscreen-Quad mit FBM-Shader — fließende
 * Nebelbänder in der App-Palette, dezente Maus-Parallaxe und ein leichtes
 * Aufglühen mit der Sprechamplitude (gleiche Analyser-Engine wie der
 * Player). Bewusst dunkel gehalten, damit Glas-Panels lesbar bleiben.
 *
 * Verhalten: DPR-Deckel 1.5, Pause bei verstecktem Tab, ein statisches
 * Frame bei prefers-reduced-motion, sauberes Dispose beim Unmount.
 */

const FRAG = /* glsl */ `
precision highp float;

uniform float uTime;
uniform vec2 uRes;
uniform vec2 uMouse;
uniform float uAmp;

// Simplex-Noise (Ashima / IQ, gemeinfrei)
vec3 permute(vec3 x) { return mod(((x * 34.0) + 1.0) * x, 289.0); }
float snoise(vec2 v) {
  const vec4 C = vec4(0.211324865405187, 0.366025403784439, -0.577350269189626, 0.024390243902439);
  vec2 i = floor(v + dot(v, C.yy));
  vec2 x0 = v - i + dot(i, C.xx);
  vec2 i1 = (x0.x > x0.y) ? vec2(1.0, 0.0) : vec2(0.0, 1.0);
  vec4 x12 = x0.xyxy + C.xxzz;
  x12.xy -= i1;
  i = mod(i, 289.0);
  vec3 p = permute(permute(i.y + vec3(0.0, i1.y, 1.0)) + i.x + vec3(0.0, i1.x, 1.0));
  vec3 m = max(0.5 - vec3(dot(x0, x0), dot(x12.xy, x12.xy), dot(x12.zw, x12.zw)), 0.0);
  m = m * m; m = m * m;
  vec3 x = 2.0 * fract(p * C.www) - 1.0;
  vec3 h = abs(x) - 0.5;
  vec3 ox = floor(x + 0.5);
  vec3 a0 = x - ox;
  m *= 1.79284291400159 - 0.85373472095314 * (a0 * a0 + h * h);
  vec3 g;
  g.x = a0.x * x0.x + h.x * x0.y;
  g.yz = a0.yz * x12.xz + h.yz * x12.yw;
  return 130.0 * dot(m, g);
}

float hash21(vec2 p) {
  return fract(sin(p.x * 127.1 + p.y * 311.7) * 43758.5453);
}

// Ein Aurora-Vorhang: wellige Basislinie, helle kompakte Unterkante (die
// Signatur echter Auroren), hohe filament-modulierte Strahlen darüber,
// Farbverlauf Teal → Violett → Magenta entlang der Höhe.
vec3 curtain(
  vec2 uv, float x, float t, float phase, float baseY, float tailK,
  float drift, float freq, vec3 hueLo, vec3 hueMid, vec3 hueHi, float gain
) {
  float yb = baseY
    + 0.055 * snoise(vec2(x * 0.9 + phase + t * drift, phase * 3.1))
    + 0.028 * snoise(vec2(x * 2.2 - t * drift * 0.7, phase * 7.7));
  float dy = uv.y - yb;
  float up = max(dy, 0.0);

  float f1 = snoise(vec2(x * freq + phase * 13.0 + t * drift * 2.0 + up * 1.5, phase * 11.0));
  float f2 = snoise(vec2(x * freq * 2.3 - t * drift * 1.3 + up * 3.0, phase * 5.0));
  float fil = clamp(0.62 * f1 + 0.38 * f2 + 0.5, 0.0, 1.0);

  float edge = exp(-abs(dy) * (dy > 0.0 ? 34.0 : 90.0)) * (0.45 + 0.55 * pow(fil, 0.7));
  float rays = (dy > 0.0 ? exp(-up * tailK) : 0.0) * pow(fil, 2.2);
  float inten = (edge * 1.15 + rays * 0.85) * gain;

  float hgt = clamp(up * 2.0, 0.0, 1.0);
  vec3 c = hueLo * pow(1.0 - hgt, 1.5)
    + hueMid * (hgt * (1.0 - hgt) * 4.0 * 0.85)
    + hueHi * (pow(hgt, 2.0) * 0.8);
  return c * inten;
}

void main() {
  vec2 uv = gl_FragCoord.xy / uRes; // y: 0 unten, 1 oben
  float aspect = uRes.x / uRes.y;
  // Deckel auf die x-Skala: Ultrawide streckt die Vorhänge,
  // statt mehr Wellenlinien zu zeigen
  float x = uv.x * min(aspect, 1.75) + uMouse.x * 0.04;
  float t = uTime;

  // Himmel: sehr dunkler Verlauf Indigo → Schwarz
  vec3 skyTop = vec3(0.022, 0.018, 0.055);
  vec3 skyBot = vec3(0.004, 0.004, 0.012);
  vec3 col = skyBot + (skyTop - skyBot) * pow(uv.y, 1.4);

  // Sterne: spärlich, mit leichtem Funkeln
  vec2 sp = vec2(uv.x * aspect, uv.y) * 220.0;
  vec2 cell = floor(sp);
  float h = hash21(cell);
  if (h > 0.995) {
    vec2 f = fract(sp);
    float d = length(f - 0.5);
    float star = pow(clamp(1.0 - d * 4.0, 0.0, 1.0), 3.0) * ((h - 0.995) / 0.005);
    float twinkle = 0.7 + 0.3 * sin(t * 2.0 + h * 80.0);
    col += star * twinkle * (0.5 + 0.5 * uv.y) * vec3(0.75, 0.8, 1.0) * 0.5;
  }

  // Drei Vorhang-Lagen mit Parallaxe; die vorderste glüht mit der Stimme
  vec3 teal = vec3(0.07, 0.66, 0.60);
  vec3 violet = vec3(0.45, 0.30, 1.00);
  vec3 magenta = vec3(0.60, 0.12, 0.55);
  col += curtain(uv, x, t, 0.0, 0.80, 3.6, 0.020, 6.0, teal, violet, magenta, 0.34 * (1.0 + uAmp * 0.5));
  col += curtain(uv, x, t, 2.7, 0.90, 4.6, 0.014, 9.0, vec3(0.10, 0.55, 0.75), violet, magenta, 0.20);
  col += curtain(uv, x, t, 5.3, 0.68, 5.2, 0.028, 4.2, teal, vec3(0.35, 0.25, 0.85), magenta, 0.13);

  // Sanfter Boden-Fade — die Krone bleibt oben am hellsten, aber die
  // gesamte Höhe bleibt bespielt statt in Schwarz abzureißen.
  col *= 0.32 + 0.68 * smoothstep(0.02, 0.55, uv.y);
  // Bodendunst: schwaches violettes Feld über die untere Bildhälfte,
  // langsam ziehend — füllt den Content-Bereich, ohne ihn aufzuhellen.
  float haze = smoothstep(0.62, 0.0, uv.y);
  float hn = 0.55 + 0.45 * snoise(vec2(x * 1.1 + t * 0.012, uv.y * 2.0 + 4.7));
  col += vec3(0.085, 0.06, 0.20) * haze * hn * 0.55;
  col += vec3(0.03, 0.10, 0.11) * smoothstep(0.35, 0.0, uv.y)
    * (0.5 + 0.5 * snoise(vec2(x * 0.7 - t * 0.008, 9.3))) * 0.4;
  float r = length(vec2((uv.x - 0.5) * 0.9, ((1.0 - uv.y) - 0.30) * 1.1));
  col *= 1.0 - smoothstep(0.6, 1.3, r) * 0.45;

  // Weiches Tonemap-Knie + Gamma, Dithering gegen Banding
  col = col / (1.0 + col * 0.6);
  col = pow(col, vec3(1.0 / 1.1));
  float dither = fract(sin(dot(gl_FragCoord.xy, vec2(12.9898, 78.233))) * 43758.5453) / 255.0;
  gl_FragColor = vec4(col + dither, 1.0);
}
`;

const VERT = /* glsl */ `
void main() { gl_Position = vec4(position, 1.0); }
`;

export default function ThreeAurora() {
  const hostRef = useRef<HTMLDivElement>(null);
  const failedRef = useRef(false);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: false, alpha: false, powerPreference: 'low-power' });
    } catch {
      failedRef.current = true;
      host.classList.add('three-aurora-failed');
      return;
    }

    // Precompile-Wächter: kompiliert der Fragment-Shader nicht (Treiber-Marotte,
    // Tippfehler), fallen wir auf die CSS-Aurora zurück statt schwarz zu bleiben.
    try {
      const gl = renderer.getContext();
      const probe = gl.createShader(gl.FRAGMENT_SHADER);
      if (!probe) throw new Error('shader alloc failed');
      gl.shaderSource(probe, FRAG);
      gl.compileShader(probe);
      const ok = gl.getShaderParameter(probe, gl.COMPILE_STATUS);
      const log = ok ? '' : String(gl.getShaderInfoLog(probe));
      gl.deleteShader(probe);
      if (!ok) throw new Error(log);
    } catch (exc) {
      console.warn('ThreeAurora: Shader kompiliert nicht — CSS-Fallback aktiv.', exc);
      renderer.dispose();
      failedRef.current = true;
      host.classList.add('three-aurora-failed');
      return;
    }

    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5));
    renderer.setSize(window.innerWidth, window.innerHeight);
    host.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
    const uniforms = {
      uTime: { value: 0 },
      uRes: { value: new THREE.Vector2(renderer.domElement.width, renderer.domElement.height) },
      uMouse: { value: new THREE.Vector2(0, 0) },
      uAmp: { value: 0 },
    };
    const material = new THREE.ShaderMaterial({ fragmentShader: FRAG, vertexShader: VERT, uniforms });
    scene.add(new THREE.Mesh(new THREE.PlaneGeometry(2, 2), material));

    const mouseTarget = new THREE.Vector2(0, 0);
    const onMouse = (e: MouseEvent) => {
      mouseTarget.set(
        (e.clientX / window.innerWidth - 0.5) * 2,
        -(e.clientY / window.innerHeight - 0.5) * 2,
      );
    };
    const onResize = () => {
      renderer.setSize(window.innerWidth, window.innerHeight);
      uniforms.uRes.value.set(renderer.domElement.width, renderer.domElement.height);
      if (reducedMotion) renderer.render(scene, camera);
    };
    window.addEventListener('mousemove', onMouse, { passive: true });
    window.addEventListener('resize', onResize);

    // Amplitude aus der Player-Engine — geglättet, damit nichts flackert
    let ampTarget = 0;
    const unsubscribe = subscribeReactive(({ amplitude }) => { ampTarget = amplitude; });

    let raf = 0;
    const start = performance.now();
    const frame = () => {
      raf = requestAnimationFrame(frame);
      if (document.hidden) return;
      uniforms.uTime.value = (performance.now() - start) / 1000;
      uniforms.uMouse.value.lerp(mouseTarget, 0.03);
      uniforms.uAmp.value += (ampTarget - uniforms.uAmp.value) * 0.06;
      ampTarget *= 0.985; // ohne Abo-Ticks langsam zurückfallen
      renderer.render(scene, camera);
    };

    if (reducedMotion) {
      // Ein hübsches statisches Frame, keine Animationsschleife
      uniforms.uTime.value = 42;
      renderer.render(scene, camera);
    } else {
      raf = requestAnimationFrame(frame);
    }

    return () => {
      cancelAnimationFrame(raf);
      unsubscribe();
      window.removeEventListener('mousemove', onMouse);
      window.removeEventListener('resize', onResize);
      material.dispose();
      renderer.dispose();
      renderer.domElement.remove();
    };
  }, []);

  return <div ref={hostRef} className="three-aurora" aria-hidden="true" />;
}
