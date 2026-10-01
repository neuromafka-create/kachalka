const listEl = document.getElementById("list");
const emptyEl = document.getElementById("empty");
const bannerEl = document.getElementById("banner");
const statusEl = document.getElementById("status");
const downloadBtn = document.getElementById("download");
const toggleBtn = document.getElementById("toggle-all");
const toolsEl = document.getElementById("tools");
const leadEl = document.getElementById("lead");

let server = null;
let videos = [];

function collectInPage() {
  const scan = globalThis.KachalkaScan;
  if (!scan) return [];

  function jsonLdUrls() {
    const out = [];
    const nodes = document.querySelectorAll('script[type="application/ld+json"]');
    function walk(node) {
      if (!node || out.length > 20) return;
      if (Array.isArray(node)) {
        node.forEach(walk);
        return;
      }
      if (typeof node !== "object") return;
      const type = node["@type"];
      const types = Array.isArray(type) ? type : [type];
      const isVideo = types.some(function (item) {
        return String(item || "").toLowerCase() === "videoobject";
      });
      if (isVideo) {
        ["embedUrl", "contentUrl", "url"].forEach(function (key) {
          if (typeof node[key] === "string") out.push(node[key]);
        });
      }
      if (node["@graph"]) walk(node["@graph"]);
    }
    nodes.forEach(function (el) {
      try {
        walk(JSON.parse(el.textContent || ""));
      } catch (_err) {
        /* чужой JSON на странице */
      }
    });
    return out;
  }

  const videosOnPage = Array.prototype.map.call(document.querySelectorAll("video"), function (el) {
    return {
      currentSrc: el.currentSrc || "",
      src: el.src || "",
      title: el.getAttribute("title") || el.getAttribute("aria-label") || "",
      duration: Number.isFinite(el.duration) ? el.duration : 0,
      sources: Array.prototype.map.call(el.querySelectorAll("source"), function (source) {
        return source.src || source.getAttribute("src") || "";
      }),
    };
  });
  const iframes = Array.prototype.map.call(document.querySelectorAll("iframe"), function (el) {
    return {
      src: el.src || "",
      dataSrc: el.getAttribute("data-src") || el.getAttribute("data-lazy-src") || "",
      title: el.getAttribute("title") || el.getAttribute("aria-label") || "",
    };
  });
  const metas = Array.prototype.map.call(
    document.querySelectorAll(
      'meta[property="og:video"], meta[property="og:video:url"], meta[property="og:video:secure_url"]'
    ),
    function (el) {
      return el.content || "";
    }
  );
  return scan.collectVideos({
    href: location.href,
    title: document.title || "",
    videos: videosOnPage,
    iframes: iframes,
    metas: metas,
    jsonLd: jsonLdUrls(),
  });
}

function videosWord(count) {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return "ролик";
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return "ролика";
  return "роликов";
}

function selectedUrls() {
  return Array.prototype.map
    .call(listEl.querySelectorAll("input:checked"), function (input) {
      return input.value;
    })
    .filter(Boolean);
}

function refreshDownloadButton() {
  const picked = selectedUrls();
  const ready = Boolean(server) && picked.length > 0;
  downloadBtn.disabled = !ready;
  downloadBtn.textContent = picked.length
    ? "Скачать выбранные (" + picked.length + ")"
    : "Скачать выбранные";
  if (videos.length > 1) {
    const allOn = picked.length === videos.length;
    toggleBtn.textContent = allOn ? "Снять все" : "Отметить все";
  }
}

function renderList() {
  listEl.textContent = "";
  if (!videos.length) {
    emptyEl.textContent =
      "На этой странице не вижу роликов. Если плеер ещё не открыт, включи его и нажми на значок ещё раз.";
    emptyEl.classList.remove("hidden");
    toolsEl.classList.add("hidden");
    refreshDownloadButton();
    return;
  }
  emptyEl.classList.add("hidden");
  toolsEl.classList.toggle("hidden", videos.length < 2);
  leadEl.textContent = videos.length > 1
    ? "Несколько роликов — отметь, какие скачать"
    : "На странице один ролик";
  videos.forEach(function (video, index) {
    const label = document.createElement("label");
    label.className = "row";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = video.url;
    input.checked = videos.length === 1;
    input.addEventListener("change", refreshDownloadButton);
    const text = document.createElement("span");
    const title = document.createElement("span");
    title.className = "title";
    title.textContent = video.title || "Видео " + (index + 1);
    const address = document.createElement("span");
    address.className = "url";
    address.textContent = video.url;
    text.appendChild(title);
    text.appendChild(address);
    label.appendChild(input);
    label.appendChild(text);
    listEl.appendChild(label);
  });
  refreshDownloadButton();
}

async function findServer() {
  const ports = [];
  for (let port = 8031; port <= 8050; port += 1) ports.push(port);
  const hits = await Promise.all(ports.map(async function (port) {
    try {
      const res = await fetch("http://127.0.0.1:" + port + "/api/config", {
        signal: AbortSignal.timeout(500),
      });
      if (!res.ok) return null;
      const data = await res.json();
      if (!data || !Object.prototype.hasOwnProperty.call(data, "default_folder")) return null;
      return { port: port, config: data };
    } catch (_err) {
      return null;
    }
  }));
  return hits.find(Boolean) || null;
}

async function videosFromTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || tab.id === undefined) {
    throw new Error("Не вижу открытую вкладку");
  }
  const url = tab.url || "";
  if (/^(chrome|edge|about|browser|devtools|view-source):/i.test(url)) {
    throw new Error("Эту служебную страницу браузер не даёт читать");
  }
  await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    files: ["scan.js"],
  });
  const [injected] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: collectInPage,
  });
  return (injected && injected.result) || [];
}

async function init() {
  statusEl.textContent = "Ищу ролики…";
  const [foundServer, foundVideos] = await Promise.all([
    findServer(),
    videosFromTab().catch(function (err) {
      return { error: err && err.message ? err.message : "Не удалось прочитать страницу" };
    }),
  ]);
  server = foundServer;
  if (!server) {
    bannerEl.textContent = "Открой программу Качалка — без неё скачивать некуда.";
    bannerEl.classList.remove("hidden");
  }
  if (foundVideos && foundVideos.error) {
    emptyEl.textContent = foundVideos.error;
    emptyEl.classList.remove("hidden");
    statusEl.textContent = "";
    refreshDownloadButton();
    return;
  }
  videos = Array.isArray(foundVideos) ? foundVideos : [];
  statusEl.textContent = "";
  renderList();
}

toggleBtn.addEventListener("click", function () {
  const boxes = listEl.querySelectorAll("input[type=checkbox]");
  const allOn = selectedUrls().length === videos.length;
  Array.prototype.forEach.call(boxes, function (box) {
    box.checked = !allOn;
  });
  refreshDownloadButton();
});

downloadBtn.addEventListener("click", async function () {
  const urls = selectedUrls();
  if (!server || !urls.length) return;
  downloadBtn.disabled = true;
  statusEl.textContent = "Отправляю в Качалку…";
  try {
    const res = await fetch("http://127.0.0.1:" + server.port + "/api/download-many", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls: urls }),
    });
    if (res.status === 404) {
      statusEl.style.color = "var(--err)";
      statusEl.textContent = "Эта Качалка старая. Запусти обновлённую программу.";
      return;
    }
    const data = await res.json();
    if (!data.ok) {
      statusEl.style.color = "var(--err)";
      statusEl.textContent = data.error || "Не удалось начать скачивание";
      return;
    }
    const count = Number(data.accepted) || urls.length;
    statusEl.style.color = "var(--ok)";
    statusEl.textContent = count === 1
      ? "Качалка приняла ролик. Прогресс виден в окне программы."
      : "Качалка приняла " + count + " " + videosWord(count) + ". Они скачаются по очереди, прогресс — в окне программы.";
  } catch (_err) {
    statusEl.style.color = "var(--err)";
    statusEl.textContent = "Нет связи с Качалкой. Проверь, что программа открыта.";
  } finally {
    refreshDownloadButton();
  }
});

init();
