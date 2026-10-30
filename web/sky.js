// Процедурные 3D-сцены на WebGL2: небо, горы, лес, луг.
// Объёмные облака: raymarching + свет по Беру—Ламберту, фаза Хеньи—Гринстейна, «пудра» на краях.
// API: Sky.setActivity(0..1), Sky.setMode('chat'|'agent'), Sky.setTheme(name), Sky.pulse(), Sky.THEMES

const VERT = `#version 300 es
in vec2 p; void main(){ gl_Position = vec4(p,0.,1.); }`;

const COMMON = `#version 300 es
precision highp float;
out vec4 o;
uniform vec2  uRes;
uniform float uTime, uClock, uMode, uAct, uSteps, uAlt;
uniform vec2  uLook;

float hash(vec3 p){ p = fract(p*0.3183099 + .1); p *= 17.0; return fract(p.x*p.y*p.z*(p.x+p.y+p.z)); }
float noise(vec3 x){
  vec3 i = floor(x), f = fract(x); f = f*f*(3.-2.*f);
  return mix(mix(mix(hash(i),            hash(i+vec3(1,0,0)),f.x),
                 mix(hash(i+vec3(0,1,0)),hash(i+vec3(1,1,0)),f.x),f.y),
             mix(mix(hash(i+vec3(0,0,1)),hash(i+vec3(1,0,1)),f.x),
                 mix(hash(i+vec3(0,1,1)),hash(i+vec3(1,1,1)),f.x),f.y),f.z);
}
float hash2(vec2 p){ vec3 q = fract(vec3(p.xyx)*.1031); q += dot(q, q.yzx + 33.33); return fract((q.x+q.y)*q.z); }
float noise2(vec2 x){
  vec2 i = floor(x), f = fract(x); vec2 u = f*f*(3.-2.*f);
  return mix(mix(hash2(i), hash2(i+vec2(1,0)), u.x), mix(hash2(i+vec2(0,1)), hash2(i+vec2(1,1)), u.x), u.y);
}
const mat2 M2 = mat2(1.6, 1.2, -1.2, 1.6);
float fbm2(vec2 p, int oct){ float f = 0., a = .5; for(int i=0;i<9;i++){ if(i>=oct) break; f += a*noise2(p); p = M2*p; a *= .5; } return f; }

float pathX(float z){ return sin(z*0.031)*8.0 + sin(z*0.0113 + 1.3)*14.0; }
float pathDX(float z){ return cos(z*0.031)*0.248 + cos(z*0.0113 + 1.3)*0.158; }

vec3 camRay(vec2 uv, float yaw, float pitch, float roll){
  vec3 fw = normalize(vec3(sin(yaw)*cos(pitch), sin(pitch), -cos(yaw)*cos(pitch)));
  vec3 rt = normalize(cross(fw, vec3(0,1,0))), up = cross(rt, fw);
  vec2 r = vec2(uv.x*cos(roll) - uv.y*sin(roll), uv.x*sin(roll) + uv.y*cos(roll));
  return normalize(fw*1.55 + rt*r.x + up*r.y);
}

vec3 skyColor(vec3 rd, vec3 zen, vec3 hor, vec3 sun, vec3 sunCol){
  float y = max(rd.y, 0.0);
  vec3 c = mix(hor, zen, pow(y, 0.45));
  if(rd.y < 0.0) c = mix(hor, hor*0.86, clamp(-rd.y*3., 0., 1.));
  float s = max(dot(rd, sun), 0.0);
  c += sunCol*(pow(s, 900.0)*5.0 + pow(s, 64.0)*0.28 + pow(s, 8.0)*0.07);
  // перистые облака высоко в небе
  if(rd.y > 0.0){
    vec2 q = rd.xz/(rd.y + 0.12)*1.6 + vec2(uClock*0.006, uTime*0.004);
    float ci = smoothstep(0.52, 0.95, fbm2(q*vec2(0.6, 2.2), 5));
    c = mix(c, vec3(1.0), ci*0.45*smoothstep(0.0, 0.25, rd.y));
  }
  return c;
}

float phaseHG(float c, float g){ float g2 = g*g; return (1.0 - g2)/pow(1.0 + g2 - 2.0*g*c, 1.5); }

float cloudDen(vec3 p, float bot, float top, float cov, float sc, int oct){
  float h = (p.y - bot)/(top - bot);
  if(h < 0.0 || h > 1.0) return 0.0;
  vec3 q = p*sc + vec3(uClock*0.03, 0.0, uClock*0.04);
  float base = noise(q*0.22)*0.55 + noise(q*0.47 + 3.1)*0.30 + noise(q*0.95 + 7.7)*0.15;
  float hg = smoothstep(0.0, 0.18, h)*smoothstep(1.0, 0.5 - 0.3*base, h);
  float d = base*hg - (1.0 - cov);
  if(d <= 0.0) return 0.0;
  float det = 0., a = .5; vec3 r = q*2.6;
  for(int i=0;i<3;i++){ if(i>=oct) break; det += a*noise(r); r = r*2.3 + vec3(0., -uClock*0.08, 0.); a *= .5; }
  d -= det*0.27*(1.2 - h);
  return clamp(d*4.6, 0.0, 1.0);
}

// rgb — свет облаков, a — пропускание
vec4 clouds(vec3 ro, vec3 rd, float bot, float top, float cov, float sc, float dens, float tmax,
            vec3 sun, vec3 sunCol, vec3 amb, vec3 fogCol, float fogK){
  float t0, t1;
  if(abs(rd.y) < 1e-4){ if(ro.y < bot || ro.y > top) return vec4(0,0,0,1); t0 = 0.; t1 = tmax; }
  else { float a = (bot - ro.y)/rd.y, b = (top - ro.y)/rd.y; t0 = max(min(a,b), 0.); t1 = min(max(a,b), tmax); }
  if(t1 <= t0) return vec4(0,0,0,1);
  float dt = clamp((t1 - t0)/uSteps, 0.07, 3.0);
  float cs = dot(rd, sun);
  float phase = mix(phaseHG(cs, 0.6), phaseHG(cs, -0.2), 0.4);
  float lstep = (top - bot)*0.09;
  vec3 col = vec3(0); float T = 1.0;
  float t = t0 + dt*hash(vec3(gl_FragCoord.xy, fract(uClock)*100.));
  for(int i=0;i<128;i++){
    if(float(i) >= uSteps || t > t1 || T < 0.03) break;
    vec3 p = ro + rd*t;
    int lod = t < 25.0 ? 3 : (t < 70.0 ? 2 : 1);
    float d = cloudDen(p, bot, top, cov, sc, lod);
    if(d > 0.002){
      float od = 0.0;
      for(int j=1;j<=4;j++){ float fj = float(j); od += cloudDen(p + sun*lstep*fj*fj*0.6, bot, top, cov, sc, 1)*fj; }
      float sig = d*dens;
      float beer = exp(-od*dens*lstep*0.9) ;
      float beer2 = exp(-od*dens*lstep*0.25)*0.35;            // многократное рассеяние (дешёвое)
      float powder = 1.0 - exp(-sig*3.0);
      float h = clamp((p.y - bot)/(top - bot), 0., 1.);
      vec3 S = sunCol*(beer + beer2)*phase*mix(0.6, 1.0, powder)*1.25 + amb*mix(0.6, 1.1, h);
      float tr = exp(-sig*dt);
      vec3 Sint = S*(1.0 - tr);
      Sint = mix(Sint, fogCol*(1.0 - tr), 1.0 - exp(-fogK*t));
      col += T*Sint; T *= tr;
    }
    t += dt;
  }
  return vec4(col, T);
}

vec3 post(vec3 col, vec3 rd, vec3 sun, vec3 sunCol){
  float s = max(dot(rd, sun), 0.0);
  col += sunCol*pow(s, 14.0)*(0.10 + 0.12*uAct);
  vec2 q = gl_FragCoord.xy/uRes;
  col *= 0.9 + 0.1*pow(16.0*q.x*q.y*(1.0-q.x)*(1.0-q.y), 0.22);
  col = col*1.04/(1.0 + col*0.04);
  return pow(clamp(col, 0., 1.), vec3(0.96));
}

vec2 screenUV(){ return (gl_FragCoord.xy*2.0 - uRes)/uRes.y; }
`;

// ---------------------------------------------------------------- НЕБО
const SKY = `
void main(){
  vec2 uv = screenUV();
  vec3 ro = vec3(0.0, uAlt + 0.1*sin(uClock*0.21), -uTime);
  float yaw = uLook.x*0.18 + sin(uClock*0.05)*0.04;
  float pitch = 0.02 + uLook.y*0.08 + 0.05*uMode;
  vec3 rd = camRay(uv, yaw, pitch, sin(uClock*0.13)*0.02 - uLook.x*0.03);
  vec3 sun = normalize(vec3(0.62, 0.30 + 0.08*uMode, -0.8));
  vec3 sunCol = vec3(1.0, 0.95, 0.86);
  vec3 zen = mix(vec3(0.13,0.42,0.88), vec3(0.07,0.28,0.74), uMode);
  vec3 hor = mix(vec3(0.74,0.87,1.00), vec3(0.66,0.82,1.00), uMode);
  vec3 bg = skyColor(rd, zen, hor, sun, sunCol);
  vec4 c = clouds(ro, rd, 0.0, 3.4, 0.56, 1.0, 2.2, 95.0, sun, sunCol, mix(hor, zen, 0.35)*0.85, hor, 0.016);
  vec3 col = bg*c.a + c.rgb;
  o = vec4(post(col, rd, sun, sunCol), 1.0);
}`;

// ---------------------------------------------------------------- ГОРЫ
const MOUNTAINS = `
float terrain(vec2 p, int oct){
  vec2 q = p*0.02; float h = 0., a = 0.5;
  for(int i=0;i<9;i++){ if(i>=oct) break; float n = noise2(q); n = 1.0 - abs(2.0*n - 1.0); h += a*n*n; q = M2*q; a *= 0.5; }
  h = h*42.0 - 7.0;
  float v = abs(p.x - pathX(p.y));
  return mix(-2.0 + 1.2*noise2(p*0.06), h, smoothstep(5.0, 42.0, v));
}
vec3 tNormal(vec3 p, float t){
  float e = 0.01 + 0.0015*t;
  return normalize(vec3(terrain(p.xz - vec2(e,0), 8) - terrain(p.xz + vec2(e,0), 8), 2.0*e,
                        terrain(p.xz - vec2(0,e), 8) - terrain(p.xz + vec2(0,e), 8)));
}
void main(){
  vec2 uv = screenUV();
  float z = -uTime*1.4;
  vec3 ro = vec3(pathX(z), 5.0 + uMode*4.0 + 0.2*sin(uClock*0.2), z);
  float yaw = atan(-pathDX(z)) + uLook.x*0.25;
  vec3 rd = camRay(uv, yaw, 0.03 + uLook.y*0.1 + uMode*0.02, -pathDX(z)*0.15);
  vec3 sun = normalize(vec3(0.55, 0.36, -0.75));
  vec3 sunCol = vec3(1.0, 0.94, 0.84);
  vec3 zen = mix(vec3(0.16,0.42,0.85), vec3(0.09,0.30,0.74), uMode);
  vec3 hor = vec3(0.74, 0.85, 0.98);
  vec3 bg = skyColor(rd, zen, hor, sun, sunCol);

  float t = 0.3, tmax = 320.0; bool hit = false;
  for(int i=0;i<180;i++){
    vec3 p = ro + rd*t;
    float h = p.y - terrain(p.xz, 5);
    if(h < 0.002*t){ hit = true; break; }
    t += max(h*0.45, 0.004*t + 0.01);
    if(t > tmax) break;
  }
  vec3 col = bg;
  if(hit){
    vec3 p = ro + rd*t, n = tNormal(p, t);
    vec3 rock = mix(vec3(0.30,0.33,0.40), vec3(0.50,0.52,0.58), noise2(p.xz*0.35));
    rock = mix(rock, vec3(0.42,0.38,0.36), smoothstep(0.4, 0.9, noise2(p.xz*0.05))*0.4);
    float snow = smoothstep(0.5, 0.78, n.y)*smoothstep(7.0, 12.0, p.y + 5.0*noise2(p.xz*0.08));
    vec3 forest = mix(vec3(0.16,0.27,0.24), vec3(0.24,0.36,0.27), noise2(p.xz*0.4));
    vec3 alb = mix(forest, rock, smoothstep(1.0, 6.0, p.y + 2.0*noise2(p.xz*0.2)));
    alb = mix(alb, vec3(0.96,0.98,1.03), snow);
    float dif = max(dot(n, sun), 0.0);
    float sh = 1.0, st = 0.5;
    for(int j=0;j<20;j++){ vec3 q = p + sun*st; float hh = q.y - terrain(q.xz, 4); sh = min(sh, 8.0*hh/st); st += clamp(hh, 0.4, 6.0); if(sh < 0.0 || st > 90.0) break; }
    sh = clamp(sh, 0.0, 1.0);
    vec3 lin = sunCol*dif*sh*1.5 + mix(hor, zen, 0.5)*(0.45 + 0.4*n.y) + vec3(0.08)*(1.0 - n.y);
    col = alb*lin;
    col = mix(col, hor*1.02, 1.0 - exp(-t*0.0085));
  }
  vec4 mist = clouds(ro, rd, -3.0, 0.6, 0.5, 0.75, 1.2, hit ? t : 220.0, sun, sunCol, hor*0.95, hor, 0.012);
  col = col*mist.a + mist.rgb;
  if(!hit && rd.y > 0.0){
    vec4 hi = clouds(ro, rd, 26.0, 36.0, 0.46, 0.32, 0.9, 400.0, sun, sunCol, mix(hor, zen, 0.4), hor, 0.004);
    col = col*hi.a + hi.rgb;
  }
  o = vec4(post(col, rd, sun, sunCol), 1.0);
}`;

// ---------------------------------------------------------------- ЛЕС
const FOREST = `
float ground(vec2 p){
  float g = 3.2*fbm2(p*0.025, 4) - 1.4;
  return mix(-1.0, g, smoothstep(2.0, 9.0, abs(p.x - pathX(p.y))));
}
float trees(vec2 p){
  vec2 c = floor(p/2.3); float best = 0.0;
  for(int j=-1;j<=1;j++) for(int i=-1;i<=1;i++){
    vec2 cc = c + vec2(i,j);
    vec2 tp = (cc + 0.15 + 0.7*vec2(hash2(cc), hash2(cc + 17.3)))*2.3;
    if(abs(tp.x - pathX(tp.y)) < 4.5) continue;
    float ht = 4.0 + 4.5*hash2(cc + 31.7);
    float d = length(p - tp);
    float cone = ht*(1.0 - d/(ht*0.27));
    cone -= 0.45*fract(cone*0.85)*step(1.0, cone);   // ярусы ели
    best = max(best, cone);
  }
  return best;
}
float H(vec2 p){ return ground(p) + trees(p); }
void main(){
  vec2 uv = screenUV();
  float z = -uTime*1.1;
  vec3 ro = vec3(pathX(z), 10.5 + uMode*3.0 + 0.15*sin(uClock*0.2), z);
  float yaw = atan(-pathDX(z)) + uLook.x*0.25;
  vec3 rd = camRay(uv, yaw, -0.12 + uLook.y*0.1 + uMode*0.03, -pathDX(z)*0.12);
  vec3 sun = normalize(vec3(0.5, 0.28, -0.82));
  vec3 sunCol = vec3(1.0, 0.93, 0.8);
  vec3 zen = mix(vec3(0.20,0.46,0.86), vec3(0.11,0.33,0.76), uMode);
  vec3 hor = vec3(0.80, 0.89, 0.97);
  vec3 bg = skyColor(rd, zen, hor, sun, sunCol);

  float t = 0.3, tmax = 220.0; bool hit = false;
  for(int i=0;i<200;i++){
    vec3 p = ro + rd*t;
    float h = p.y - H(p.xz);
    if(h < 0.003*t){ hit = true; break; }
    t += max(h*0.24, 0.01 + 0.002*t);
    if(t > tmax) break;
  }
  vec3 col = bg;
  if(hit){
    vec3 p = ro + rd*t;
    float e = 0.02 + 0.001*t;
    vec3 n = normalize(vec3(H(p.xz - vec2(e,0)) - H(p.xz + vec2(e,0)), 2.0*e, H(p.xz - vec2(0,e)) - H(p.xz + vec2(0,e))));
    float g = ground(p.xz), tr = p.y - g;
    float dif = max(dot(n, sun), 0.0);
    vec3 amb = mix(hor, zen, 0.5);
    if(tr > 0.15){                               // хвоя
      float k = hash2(floor(p.xz/2.3));
      vec3 alb = mix(vec3(0.07,0.19,0.16), vec3(0.13,0.27,0.18), k);
      alb *= 0.75 + 0.5*smoothstep(0.0, 7.0, tr);
      col = alb*(sunCol*dif*1.6 + amb*(0.55 + 0.35*n.y));
    } else if(abs(p.x - pathX(p.z)) < 2.2 && g < -0.6){   // река
      vec3 rf = reflect(rd, vec3(0,1,0));
      float fr = 0.25 + 0.75*pow(1.0 - max(-rd.y, 0.0), 5.0);
      col = mix(vec3(0.10,0.22,0.26), skyColor(rf, zen, hor, sun, sunCol), fr);
      col += 0.04*noise2(p.xz*vec2(3., 0.4) + vec2(0., uClock*2.));
    } else {                                     // мох и трава
      vec3 alb = mix(vec3(0.20,0.30,0.18), vec3(0.34,0.42,0.24), noise2(p.xz*0.5));
      col = alb*(sunCol*dif*1.4 + amb*(0.5 + 0.4*n.y));
    }
    col = mix(col, hor, 1.0 - exp(-t*0.013));
  }
  vec4 mist = clouds(ro, rd, -1.4, 2.6, 0.46, 0.9, 0.8, hit ? t : 160.0, sun, sunCol, hor, hor, 0.01);
  col = col*mist.a + mist.rgb;
  if(!hit && rd.y > 0.0){
    vec4 hi = clouds(ro, rd, 30.0, 40.0, 0.5, 0.3, 1.0, 400.0, sun, sunCol, mix(hor, zen, 0.4), hor, 0.004);
    col = col*hi.a + hi.rgb;
  }
  o = vec4(post(col, rd, sun, sunCol), 1.0);
}`;

// ---------------------------------------------------------------- ЛУГ
const MEADOW = `
float hills(vec2 p){ return 7.0*fbm2(p*0.011, 4) + 0.9*noise2(p*0.07); }
void main(){
  vec2 uv = screenUV();
  float z = -uTime*1.2;
  vec2 cxz = vec2(pathX(z)*0.6, z);
  vec3 ro = vec3(cxz.x, hills(cxz) + 1.8 + uMode*2.5 + 0.06*sin(uClock*1.7), z);
  float yaw = atan(-pathDX(z)*0.6) + uLook.x*0.25;
  vec3 rd = camRay(uv, yaw, 0.07 + uLook.y*0.1 + uMode*0.04, -pathDX(z)*0.08);
  vec3 sun = normalize(vec3(0.45, 0.5, -0.75));
  vec3 sunCol = vec3(1.0, 0.96, 0.88);
  vec3 zen = mix(vec3(0.14,0.44,0.90), vec3(0.08,0.31,0.78), uMode);
  vec3 hor = vec3(0.76, 0.88, 1.0);
  vec3 bg = skyColor(rd, zen, hor, sun, sunCol);

  float t = 0.2, tmax = 300.0; bool hit = false;
  if(rd.y < 0.12){
    for(int i=0;i<140;i++){
      vec3 p = ro + rd*t;
      float h = p.y - hills(p.xz);
      if(h < 0.002*t){ hit = true; break; }
      t += max(h*0.6, 0.01 + 0.003*t);
      if(t > tmax) break;
    }
  }
  vec3 col = bg;
  if(hit){
    vec3 p = ro + rd*t;
    float e = 0.02 + 0.001*t;
    vec3 n = normalize(vec3(hills(p.xz - vec2(e,0)) - hills(p.xz + vec2(e,0)), 2.0*e, hills(p.xz - vec2(0,e)) - hills(p.xz + vec2(0,e))));
    vec3 alb = mix(vec3(0.30,0.56,0.18), vec3(0.62,0.74,0.26), fbm2(p.xz*0.04, 3));
    float wind = noise2(p.xz*0.12 + vec2(uClock*0.9, uClock*0.35));
    alb *= 0.82 + 0.36*wind;
    alb *= 0.85 + 0.3*noise2(p.xz*3.0);
    float v = abs(p.x - pathX(p.z)*0.6);                       // тропинка
    alb = mix(alb, vec3(0.66,0.58,0.42), smoothstep(0.9, 0.5, v + 0.3*noise2(p.xz*1.5)));
    if(t < 45.0){                                              // цветы
      vec2 g = p.xz*5.0; vec2 id = floor(g);
      float f = hash2(id);
      if(f > 0.955 && length(fract(g) - 0.5) < 0.22 && v > 1.0){
        float k = hash2(id + 3.7);
        vec3 fc = k < 0.4 ? vec3(1.0) : (k < 0.7 ? vec3(1.0,0.85,0.25) : (k < 0.85 ? vec3(0.62,0.5,1.0) : vec3(1.0,0.55,0.65)));
        alb = mix(alb, fc, smoothstep(45.0, 20.0, t));
      }
    }
    float dif = max(dot(n, sun), 0.0);
    col = alb*(sunCol*dif*1.35 + mix(hor, zen, 0.5)*(0.5 + 0.35*n.y));
    col = mix(col, hor, 1.0 - exp(-t*0.011));
  }
  if(!hit && rd.y > 0.0){
    vec4 c = clouds(ro, rd, 16.0, 30.0, 0.52, 0.3, 1.1, 420.0, sun, sunCol, mix(hor, zen, 0.35), hor, 0.0035);
    col = col*c.a + c.rgb;
  }
  o = vec4(post(col, rd, sun, sunCol), 1.0);
}`;

const THEMES = {
  sky:       { name: 'Небо', src: SKY,       alt: [5.0, 6.6], steps: 76, bg: 'linear-gradient(180deg,#3d8be8,#bfe0ff 60%,#eef7ff)' },
  mountains: { name: 'Горы', src: MOUNTAINS, alt: [0, 0],     steps: 40, bg: 'linear-gradient(180deg,#3f86d8,#c3dcf5 55%,#8a96a8)' },
  forest:    { name: 'Лес',  src: FOREST,    alt: [0, 0],     steps: 36, bg: 'linear-gradient(180deg,#5a9be0,#d4e6f4 50%,#2f4a3d)' },
  meadow:    { name: 'Луг',  src: MEADOW,    alt: [0, 0],     steps: 48, bg: 'linear-gradient(180deg,#3d8be8,#cfe6ff 50%,#7fae4a)' },
};

export const Sky = (() => {
  const canvas = document.getElementById('sky');
  const gl = canvas.getContext('webgl2', { antialias: false, alpha: false, powerPreference: 'high-performance' });
  const state = { act: 0, actT: 0, mode: 0, modeT: 0, alt: 4.2, look: [0, 0], lookT: [0, 0],
                  travel: 0, scale: 0.5, quality: 1, pulse: 0, theme: null, prog: null, U: null };
  const api = { THEMES, setActivity(){}, setMode(){}, setTheme(){}, pulse(){}, setQuality(){} };
  if (!gl) { document.body.classList.add('no-webgl'); return api; }

  const sh = (type, src) => {
    const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) console.error(gl.getShaderInfoLog(s));
    return s;
  };
  const buf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 3,-1, -1,3]), gl.STATIC_DRAW);
  const cache = {};
  function program(name) {
    if (cache[name]) return cache[name];
    const prog = gl.createProgram();
    gl.attachShader(prog, sh(gl.VERTEX_SHADER, VERT));
    gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, COMMON + THEMES[name].src));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) console.error(gl.getProgramInfoLog(prog));
    const U = {};
    for (const n of ['uRes','uTime','uClock','uLook','uAlt','uMode','uAct','uSteps']) U[n] = gl.getUniformLocation(prog, n);
    return (cache[name] = { prog, U, loc: gl.getAttribLocation(prog, 'p') });
  }
  function use(name) {
    const p = program(name);
    gl.useProgram(p.prog);
    gl.enableVertexAttribArray(p.loc); gl.vertexAttribPointer(p.loc, 2, gl.FLOAT, false, 0, 0);
    state.prog = p; state.theme = name;
    document.body.style.background = THEMES[name].bg;
  }

  const resize = () => {
    const w = Math.max(1, Math.floor(innerWidth * state.scale)), h = Math.max(1, Math.floor(innerHeight * state.scale));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; gl.viewport(0, 0, w, h); }
  };
  addEventListener('resize', resize);
  addEventListener('pointermove', e => { state.lookT = [(e.clientX / innerWidth) * 2 - 1, -((e.clientY / innerHeight) * 2 - 1)]; });

  let last = performance.now(), acc = 0, frames = 0, start = last, stepMul = 1;
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  function frame(now) {
    const dt = Math.min(0.05, (now - last) / 1000); last = now;
    acc += dt; frames++;
    if (acc > 1.5) {   // адаптивное качество: держим плавность
      const fps = frames / acc, maxS = 0.4 + 0.25 * state.quality;
      if (fps < 42 && state.scale > 0.22) { state.scale = Math.max(0.22, state.scale - 0.05); stepMul = Math.max(0.6, stepMul - 0.1); }
      else if (fps > 57 && state.scale < maxS) { state.scale = Math.min(maxS, state.scale + 0.03); stepMul = Math.min(1.2, stepMul + 0.05); }
      acc = 0; frames = 0;
    }
    if (!state.prog) { requestAnimationFrame(frame); return; }
    resize();
    const th = THEMES[state.theme];
    state.act += (state.actT - state.act) * (1 - Math.exp(-dt * 2.2));
    state.mode += (state.modeT - state.mode) * (1 - Math.exp(-dt * 1.2));
    const altT = th.alt[state.modeT ? 1 : 0];
    state.alt += (altT - state.alt) * (1 - Math.exp(-dt * 0.9));
    for (const i of [0, 1]) state.look[i] += (state.lookT[i] - state.look[i]) * (1 - Math.exp(-dt * 1.5));
    state.pulse *= Math.exp(-dt * 2.5);
    state.travel += dt * (reduce ? 0.15 : 0.9 + state.act * 4.0 + state.pulse * 5);
    const U = state.prog.U;
    gl.uniform2f(U.uRes, canvas.width, canvas.height);
    gl.uniform1f(U.uTime, state.travel);
    gl.uniform1f(U.uClock, reduce ? 0 : (now - start) / 1000);
    gl.uniform2f(U.uLook, state.look[0], state.look[1]);
    gl.uniform1f(U.uAlt, state.alt);
    gl.uniform1f(U.uMode, state.mode);
    gl.uniform1f(U.uAct, state.act);
    gl.uniform1f(U.uSteps, Math.round(th.steps * stepMul * (0.75 + 0.25 * state.quality)));
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    requestAnimationFrame(frame);
  }

  let switching = null;
  Object.assign(api, {
    setActivity(v) { state.actT = v; },
    setMode(m) { state.modeT = m === 'agent' ? 1 : 0; state.pulse = 1; },
    pulse() { state.pulse = Math.min(1, state.pulse + 0.6); },
    setQuality(q) { state.quality = q; },     // 0 экономия, 1 авто, 2 максимум
    setTheme(name) {
      if (!THEMES[name]) name = 'sky';
      if (!state.theme) { use(name); state.alt = THEMES[name].alt[0]; return; }
      if (name === state.theme) return;
      clearTimeout(switching);
      canvas.style.opacity = '0';
      switching = setTimeout(() => { use(name); state.alt = THEMES[name].alt[state.modeT ? 1 : 0]; canvas.style.opacity = '1'; }, 550);
    },
  });
  resize(); requestAnimationFrame(frame);
  return api;
})();
