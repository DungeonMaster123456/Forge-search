export async function onRequest(context) {
  const { request, env } = context;
  const url = new URL(request.url);

  // Only allow GET
  if (request.method !== "GET") {
    return new Response("Method not allowed", { status: 405 });
  }

  const query = url.searchParams.get("q");
  if (!query) {
    return json({ error: "Missing ?q= parameter" }, 400);
  }

  // Optional API key protection
  const key = url.searchParams.get("key") || request.headers.get("x-api-key");
  if (env.API_KEY && key !== env.API_KEY) {
    return json({ error: "Invalid API key" }, 401);
  }

  try {
    const results = await humanDdgSearch(query);
    return json({
      query,
      count: results.length,
      results
    });
  } catch (err) {
    return json({ error: err.message }, 500);
  }
}

async function humanDdgSearch(query) {
  const target = "https://html.duckduckgo.com/html/?q=" + encodeURIComponent(query);

  // Small human-like delay
  await sleep(random(400, 1100));

  const res = await fetch(target, {
    headers: {
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      "Accept-Language": "en-US,en;q=0.9",
      "Referer": "https://html.duckduckgo.com/",
      "DNT": "1",
    }
  });

  if (res.status === 202 || res.status === 403 || res.status === 429) {
    throw new Error(`DuckDuckGo rate limited you (${res.status})`);
  }

  if (!res.ok) {
    throw new Error(`HTTP error ${res.status}`);
  }

  const html = await res.text();
  return parseResults(html);
}

function parseResults(html) {
  const results = [];
  const regex = /class="result__a"[^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>[\s\S]*?(?:class="result__snippet"[^>]*>([\s\S]*?)<\/a>)?/gi;

  let match;
  while ((match = regex.exec(html)) !== null) {
    let link = match[1];
    const title = clean(match[2]);
    const snippet = match[3] ? clean(match[3]) : "";

    // Clean uddg= redirect
    if (link.includes("uddg=")) {
      try {
        const u = new URL(link, "https://duckduckgo.com");
        link = decodeURIComponent(u.searchParams.get("uddg") || link);
      } catch (e) {}
    }

    if (title && link) {
      results.push({ title, url: link, snippet });
    }
  }

  return results.slice(0, 10);
}

function clean(text) {
  return text.replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim();
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

function random(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*"
    }
  });
}
