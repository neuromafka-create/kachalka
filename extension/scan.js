// Ролики на уже открытой странице: сама вкладка, iframe-плееры, <video>, og:video.
// Логика без DOM, чтобы её можно было проверить отдельно от браузера.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.KachalkaScan = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const MEDIA_EXT = [".mp4", ".webm", ".mkv", ".mov", ".m4v", ".ogv", ".m3u8", ".mpd"];
  const SECRET_KEYS = ["hash", "access_key", "access_hash", "p", "token", "password", "sig", "h"];
  const SKIP_HOSTS = [
    "doubleclick.net",
    "googlesyndication.com",
    "google-analytics.com",
    "googletagmanager.com",
    "mc.yandex.ru",
    "mc.yandex.com",
    "hotjar.com",
    "scorecardresearch.com",
    "facebook.net",
    "gravatar.com",
    "googlevideo.com",
  ];
  const NAME_SKIP = ["thumbnail", "thumb", "/poster", "sprite", "placeholder", "/avatar", "/logo", "/icon", "favicon"];
  const GENERIC = new Set([
    "",
    "video",
    "video player",
    "youtube",
    "youtube video player",
    "vk",
    "вк",
    "rutube",
    "vimeo",
    "player",
    "embedded video",
  ]);

  function hostOf(url) {
    try {
      return new URL(url).hostname.toLowerCase().replace(/\.$/, "");
    } catch (_err) {
      return "";
    }
  }

  function hostIs(host, suffix) {
    return host === suffix || host.endsWith("." + suffix);
  }

  function skippedHost(host) {
    return SKIP_HOSTS.some(function (suffix) {
      return hostIs(host, suffix);
    });
  }

  function mediaExt(path) {
    const low = String(path || "").toLowerCase().split("?")[0];
    for (let i = 0; i < MEDIA_EXT.length; i += 1) {
      if (low.endsWith(MEDIA_EXT[i])) return MEDIA_EXT[i];
    }
    return "";
  }

  function absUrl(raw, base) {
    const text = String(raw || "").trim();
    if (!text || text.startsWith("blob:") || text.startsWith("data:") || text.startsWith("javascript:")) {
      return "";
    }
    try {
      const url = new URL(text, base || undefined);
      if (url.protocol !== "http:" && url.protocol !== "https:") return "";
      return url.href;
    } catch (_err) {
      return "";
    }
  }

  function queryMap(url) {
    const out = {};
    try {
      new URL(url).searchParams.forEach(function (value, key) {
        if (out[key] === undefined) out[key] = value;
      });
    } catch (_err) {
      /* ignore */
    }
    return out;
  }

  function secretSuffix(url) {
    const qs = queryMap(url);
    const parts = SECRET_KEYS.filter(function (key) {
      return qs[key];
    }).map(function (key) {
      return key + "=" + qs[key];
    });
    return parts.length ? "|" + parts.join("&") : "";
  }

  function hasSecret(url) {
    return secretSuffix(url) !== "";
  }

  function isVideoUrl(url) {
    let parsed;
    try {
      parsed = new URL(url);
    } catch (_err) {
      return false;
    }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return false;
    const host = hostOf(url);
    if (!host || skippedHost(host)) return false;
    const path = parsed.pathname || "";
    const ext = mediaExt(path);
    if (ext) {
      const low = decodeURIComponent(path).toLowerCase();
      if (NAME_SKIP.some(function (bad) { return low.indexOf(bad) !== -1; })) return false;
      return true;
    }
    if (host === "youtu.be" || host.endsWith(".youtu.be")) {
      return /^\/[\w-]{6,}\/?$/.test(path);
    }
    if (hostIs(host, "youtube.com") || hostIs(host, "youtube-nocookie.com")) {
      if (/\/(?:embed|shorts|live|v)\/[\w-]{6,}/.test(path)) return true;
      if (path.replace(/\/+$/, "") === "/watch") return parsed.searchParams.has("v");
      return false;
    }
    if (hostIs(host, "vk.com") || hostIs(host, "vk.ru") || hostIs(host, "vkvideo.ru")) {
      if (/(?:video|clip)-?\d+_\d+/.test(path) || path.indexOf("video_ext.php") !== -1) return true;
      return /(?:video|clip)-?\d+_\d+/.test(parsed.search);
    }
    if (hostIs(host, "rutube.ru")) {
      return /\/(?:video|play\/embed|embed|shorts)\/[\w-]+/.test(path);
    }
    if (host === "player.vimeo.com") return /\/video\/\d+/.test(path);
    if (hostIs(host, "vimeo.com")) return /^\/(?:video\/)?\d+\/?$/.test(path);
    if (hostIs(host, "dailymotion.com")) return /\/(?:video|embed\/video)\/[\w]+/.test(path);
    if (host === "dai.ly") return /^\/[\w-]+\/?$/.test(path);
    if (hostIs(host, "ok.ru")) return /\/(?:videoembed|video|live)\/\d+/.test(path);
    if (hostIs(host, "dzen.ru")) return /\/(?:embed|video\/watch|shorts)\/[\w.-]+/.test(path);
    if (hostIs(host, "tiktok.com")) return path.indexOf("/video/") !== -1;
    if (hostIs(host, "twitch.tv")) return host.startsWith("player.") || path.indexOf("/videos/") !== -1;
    if (/\/sign-player\/?$/.test(path) && /[?&]json=/.test(parsed.search || "")) return true;
    if (kinescopeId(url)) return true;
    return false;
  }

  function kinescopeId(url) {
    let parsed;
    try {
      parsed = new URL(url);
    } catch (_err) {
      return "";
    }
    const host = hostOf(url);
    if (host !== "kinescope.io" && host !== "www.kinescope.io") return "";
    const parts = (parsed.pathname || "").split("/").filter(Boolean);
    let id = "";
    if (parts[0] && parts[0].toLowerCase() === "embed" && parts.length === 2) id = parts[1];
    else if (parts.length === 1) id = parts[0];
    else return "";
    if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)) {
      return id.toLowerCase();
    }
    if (/^[0-9A-Za-z]{10,}$/.test(id)) return id;
    return "";
  }

  function getcourseHash(url) {
    try {
      const parsed = new URL(url);
      if (!/\/sign-player\/?$/.test(parsed.pathname || "")) return "";
      const raw = parsed.searchParams.get("json") || "";
      if (!raw) return "";
      const body = raw.replace(/-/g, "+").replace(/_/g, "/");
      const pad = body + "=".repeat((4 - (body.length % 4)) % 4);
      const payload = JSON.parse(atob(pad));
      const hash = payload && payload.video_hash;
      return /^[0-9a-f]{16,64}$/i.test(hash) ? String(hash).toLowerCase() : "";
    } catch (_err) {
      return "";
    }
  }

  function knownVideoSite(url) {
    const host = hostOf(url);
    return host === "youtu.be" || host.endsWith(".youtu.be")
      || hostIs(host, "youtube.com") || hostIs(host, "youtube-nocookie.com")
      || hostIs(host, "vk.com") || hostIs(host, "vk.ru") || hostIs(host, "vkvideo.ru")
      || hostIs(host, "rutube.ru") || hostIs(host, "vimeo.com")
      || hostIs(host, "dzen.ru") || hostIs(host, "ok.ru")
      || hostIs(host, "tiktok.com") || hostIs(host, "twitch.tv")
      || hostIs(host, "dailymotion.com") || host === "dai.ly";
  }

  function canonicalKey(url) {
    let parsed;
    try {
      parsed = new URL(url);
    } catch (_err) {
      return url;
    }
    const host = hostOf(url);
    const path = parsed.pathname || "";
    const secret = secretSuffix(url);
    if (host === "youtu.be" || host.endsWith(".youtu.be")) {
      const vid = path.replace(/^\/+|\/+$/g, "").split("/")[0];
      if (/^[\w-]{6,}$/.test(vid)) return "yt:" + vid + secret;
    }
    if (host.indexOf("youtube") !== -1) {
      let vid = parsed.searchParams.get("v") || "";
      if (!vid) {
        const found = path.match(/\/(?:embed|shorts|live|v)\/([\w-]{6,})/);
        if (found) vid = found[1];
      }
      if (vid) return "yt:" + vid + secret;
    }
    if (hostIs(host, "vk.com") || hostIs(host, "vk.ru") || hostIs(host, "vkvideo.ru")) {
      const found = url.match(/(?:video|clip)(-?\d+_\d+)/);
      if (found) return "vk:" + found[1] + secret;
      const oid = parsed.searchParams.get("oid");
      const id = parsed.searchParams.get("id");
      if (oid && id) return "vk:" + oid + "_" + id + secret;
    }
    if (hostIs(host, "rutube.ru")) {
      const found = path.match(/\/(?:play\/embed|video|embed|shorts)\/([\w-]+)/);
      if (found) return "rt:" + found[1] + secret;
    }
    if (hostIs(host, "vimeo.com")) {
      const found = path.match(/\/video\/(\d+)/) || path.match(/\/(\d+)\/?$/);
      if (found) return "vm:" + found[1] + secret;
    }
    if (hostIs(host, "ok.ru")) {
      const found = path.match(/\/(?:videoembed|video|live)\/(\d+)/);
      if (found) return "ok:" + found[1] + secret;
    }
    const gc = getcourseHash(url);
    if (gc) return "gc:" + gc;
    const ks = kinescopeId(url);
    if (ks) return "ks:" + ks;
    if (mediaExt(path)) return "file:" + host + path;
    return url.split("#")[0];
  }

  function downloadUrl(url) {
    const clean = String(url || "").split("#")[0];
    if (!clean) return "";
    if (hasSecret(clean)) return clean;
    const key = canonicalKey(clean);
    const id = key.split("|")[0].slice(3);
    if (key.startsWith("yt:")) return "https://www.youtube.com/watch?v=" + id;
    if (key.startsWith("vk:")) return "https://vk.ru/video" + id;
    if (key.startsWith("rt:")) return "https://rutube.ru/video/" + id + "/";
    if (key.startsWith("vm:")) return "https://vimeo.com/" + id;
    if (key.startsWith("ok:")) return "https://ok.ru/video/" + id;
    return clean;
  }

  function serviceName(url) {
    const host = hostOf(url);
    if (host.indexOf("youtu") !== -1) return "YouTube";
    if (hostIs(host, "vk.com") || hostIs(host, "vk.ru") || hostIs(host, "vkvideo.ru")) return "ВК";
    if (hostIs(host, "rutube.ru")) return "Rutube";
    if (hostIs(host, "vimeo.com")) return "Vimeo";
    if (hostIs(host, "ok.ru")) return "OK";
    if (hostIs(host, "dailymotion.com") || host === "dai.ly") return "Dailymotion";
    if (hostIs(host, "dzen.ru")) return "Дзен";
    if (hostIs(host, "twitch.tv")) return "Twitch";
    if (hostIs(host, "tiktok.com")) return "TikTok";
    if (/\/sign-player\/?$/.test(new URL(url).pathname || "")) return "GetCourse";
    if (kinescopeId(url)) return "Kinescope";
    if (mediaExt(host ? new URL(url).pathname : "")) return "Файл на странице";
    return host || "Видео";
  }

  function cleanTitle(raw) {
    let title = String(raw || "").replace(/\s+/g, " ").trim();
    title = title.replace(/\s+[-|–—]\s+(YouTube|Rutube|VK|ВКонтакте|ВК|Vimeo|Дзен|OK\.ru).*$/i, "");
    if (GENERIC.has(title.toLowerCase())) return "";
    return title.slice(0, 140);
  }

  function displayTitle(raw, url) {
    return cleanTitle(raw) || serviceName(url);
  }

  function pushItem(items, url, title, kind) {
    const ready = downloadUrl(url);
    if (!ready || (!isVideoUrl(ready) && !isVideoUrl(url))) return;
    const finalUrl = isVideoUrl(ready) ? ready : url;
    items.push({
      url: downloadUrl(finalUrl) || finalUrl,
      title: displayTitle(title, finalUrl),
      kind: kind,
    });
  }

  const PRUFFME_PATH = /^user\/[0-9a-f]{16,64}\/video\/[0-9a-f]{16,64}(?:\/\d{3,4})?\/video\.(?:mp4|webm|mkv|mov|m4v)$/i;

  function pruffmeFile(raw) {
    const text = String(raw || "").trim();
    if (!text) return null;
    let media;
    try {
      media = JSON.parse(text);
    } catch (_err) {
      return null;
    }
    if (!media || typeof media !== "object") return null;
    let path = String(media.path || "").replace(/\\/g, "/").replace(/^\/+/, "");
    const direct = String(media.url || "");
    if (direct.indexOf("https://") === 0) {
      try {
        const parsed = new URL(direct);
        const host = parsed.hostname.toLowerCase();
        if ((host === "pruffme.com" || host.endsWith(".pruffme.com")) && !parsed.search) {
          path = decodeURIComponent(parsed.pathname || "").replace(/^\/+/, "");
        }
      } catch (_err) {
        path = String(media.path || "").replace(/\\/g, "/").replace(/^\/+/, "");
      }
    }
    if (!PRUFFME_PATH.test(path)) return null;
    return {
      url: "https://video.pruffme.com/" + path,
      title: String(media.name || ""),
    };
  }

  function collectVideos(page) {
    const href = String((page && page.href) || "");
    const pageTitle = (page && page.title) || "";
    const found = [];

    if (isVideoUrl(href)) {
      pushItem(found, href, pageTitle, "page");
    }

    const pruffme = pruffmeFile(page && page.mediaJson);
    if (pruffme) pushItem(found, pruffme.url, pruffme.title, "file");

    (page && page.iframes || []).forEach(function (frame) {
      const src = absUrl(frame && (frame.src || frame.dataSrc), href);
      if (!src || !isVideoUrl(src)) return;
      pushItem(found, src, (frame && frame.title) || "", "embed");
    });

    (page && page.videos || []).forEach(function (video) {
      const duration = Number(video && video.duration);
      if (duration > 0 && duration < 2.5) return;
      const candidates = [video && video.currentSrc, video && video.src].concat((video && video.sources) || []);
      for (let i = 0; i < candidates.length; i += 1) {
        const src = absUrl(candidates[i], href);
        if (!src || !isVideoUrl(src) || !mediaExt(new URL(src).pathname)) continue;
        pushItem(found, src, (video && video.title) || pageTitle, "file");
        break;
      }
    });

    if (!found.length) {
      (page && page.metas || []).concat(page && page.jsonLd || []).forEach(function (raw) {
        const src = absUrl(raw, href);
        if (!src || !isVideoUrl(src)) return;
        pushItem(found, src, pageTitle, "meta");
      });
    }

    // Ссылки из вёрстки — только на обычной странице и только если плеера нет.
    // На YouTube, ВК и Rutube лента рекомендаций в список не попадает.
    if (!found.length && !knownVideoSite(href)) {
      (page && page.links || []).forEach(function (link) {
        const raw = typeof link === "string" ? link : (link && (link.href || link.url));
        const title = typeof link === "string" ? "" : (link && link.title) || "";
        const src = absUrl(raw, href);
        if (!src) return;
        pushItem(found, src, title, "link");
      });
      (page && page.textUrls || []).forEach(function (raw) {
        const src = absUrl(raw, href);
        if (!src) return;
        pushItem(found, src, "", "text");
      });
    }

    const out = [];
    const seen = new Set();
    found.forEach(function (item) {
      if (!item.url || !item.url.startsWith("http")) return;
      const key = canonicalKey(item.url);
      if (seen.has(key)) return;
      seen.add(key);
      out.push({ url: item.url, title: item.title || "Видео", kind: item.kind || "" });
    });
    return out.slice(0, 12);
  }

  return { collectVideos: collectVideos, downloadUrl: downloadUrl, isVideoUrl: isVideoUrl };
});
