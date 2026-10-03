// iPoW 中转：把请求从 Cloudflare 出口转发到 ipow.ai
//
// 必须带令牌，否则一律 404 —— 避免变成谁都能白用的公开中转，把额度耗光。
//
//   https://<name>.<subdomain>.workers.dev/r/<TOKEN>/ipow/<原路径>
//
// 给 ipow.py 用就是：
//   --base-url https://<name>.<subdomain>.workers.dev/r/<TOKEN>/ipow
const UPSTREAM = "https://ipow.ai";

// 注意：Response 不能在模块顶层构造（Workers 禁止全局作用域里的这类操作），
// 必须放进 handler 里现造。
function notFound() {
  return new Response("not found", { status: 404 });
}

export default {
  async fetch(request, env) {
    const token = env.RELAY_TOKEN || "";
    // 没配令牌就一律拒绝，绝不退化成公开中转
    if (!token) {
      return notFound();
    }

    const url = new URL(request.url);
    const m = url.pathname.match(/^\/r\/([A-Za-z0-9_-]+)\/(.*)$/);
    if (!m || m[1] !== token) {
      return notFound();
    }

    const rest = m[2] || "";

    // 令牌内的小自检：回报 Worker 自己的出口 IP
    if (rest === "__ip") {
      const r = await fetch("https://api.ipify.org/?format=json");
      return new Response(await r.text(), {
        headers: { "content-type": "application/json" },
      });
    }

    if (!rest.startsWith("ipow/")) {
      return notFound();
    }
    const path = "/" + rest.slice("ipow/".length);

    const headers = new Headers();
    for (const [k, v] of request.headers) {
      const lk = k.toLowerCase();
      if (lk === "host" || lk === "cf-connecting-ip" || lk === "x-forwarded-for" ||
          lk === "cf-ray" || lk === "content-length") {
        continue;
      }
      headers.set(k, v);
    }

    const init = { method: request.method, headers, redirect: "manual" };
    if (request.method !== "GET" && request.method !== "HEAD") {
      init.body = await request.arrayBuffer();
    }

    const resp = await fetch(UPSTREAM + path + url.search, init);
    const out = new Headers(resp.headers);
    out.delete("content-encoding");
    out.delete("content-length");
    return new Response(resp.body, { status: resp.status, headers: out });
  },
};