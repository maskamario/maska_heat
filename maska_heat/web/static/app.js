// Charts for the dashboard and history pages. Times arrive as unix ms and are shown in the house's timezone
// (<html data-tz>), like every time the server writes — not the phone's, which may be elsewhere.
(function () {
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const timeZone = document.documentElement.dataset.tz || undefined;
  const partsFmt = new Intl.DateTimeFormat("en-GB", { timeZone, weekday: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const parts = (ms) => Object.fromEntries(partsFmt.formatToParts(new Date(ms)).map((p) => [p.type, p.value]));
  const hhmm = (ms) => { const p = parts(ms); return `${p.hour}:${p.minute}`; };
  const dayTime = (ms) => { const p = parts(ms); return `${p.weekday} ${p.hour}:${p.minute}`; };
  const dayDate = (ms, month) => new Date(ms).toLocaleDateString("en-GB", { timeZone, weekday: "short", day: "numeric", ...(month ? { month: "numeric" } : {}) });

  function baseOptions() {
    Chart.defaults.color = css("--muted");
    Chart.defaults.borderColor = css("--line");
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    return { responsive: true, maintainAspectRatio: false, animation: false, interaction: { mode: "index", intersect: false } };
  }

  function renderPrices(canvas, slots) {
    // now = ink, spike hours = ochre, night tariff = frost, day tariff = neutral
    const colors = slots.map((s) => (s.is_now ? css("--ink") : s.is_spike ? css("--ochre") : s.is_day ? css("--bar-day") : css("--frost")));
    new Chart(canvas, {
      type: "bar",
      data: {
        labels: slots.map((s) => s.ts * 1000),
        datasets: [{ data: slots.map((s) => +(s.cost_total * 100).toFixed(2)), backgroundColor: colors, borderRadius: 3 }],
      },
      options: {
        ...baseOptions(),
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: (items) => dayTime(+items[0].label),
              label: (item) => {
                const s = slots[item.dataIndex];
                return [`${item.raw.toFixed(1)} c/kWh total`, `spot ${(s.eur_mwh / 10).toFixed(1)} c/kWh`, s.is_day ? "day tariff" : "night tariff"];
              },
            },
          },
        },
        scales: {
          // a label every 3 hours (6 on a phone), on the whole hour only (also with 15-min slots); the day name only at midnight
          x: { ticks: { callback: function (v) { const ms = +this.getLabelForValue(v); const p = parts(ms); const every = canvas.clientWidth < 500 ? 6 : 3; if (+p.hour % every !== 0 || p.minute !== "00") return ""; return p.hour === "00" ? p.weekday : p.hour; }, autoSkip: false, maxRotation: 0 }, grid: { display: false } },
          y: { beginAtZero: true, title: { display: true, text: "c/kWh" } },
        },
      },
    });
  }

  function lineSet(label, points, color, extra = {}) {
    return { label, data: points.map(([x, y]) => ({ x, y })), borderColor: color, backgroundColor: color, pointRadius: 0, borderWidth: 2, tension: 0.2, ...extra };
  }

  function timeAxis(min, max) {
    return { type: "linear", min, max, ticks: { callback: (v) => { const p = parts(v); return p.hour === "00" && p.minute === "00" ? dayDate(v) : hhmm(v); }, maxTicksLimit: 10 } };
  }

  async function renderHistory({ zone, hours }) {
    const res = await fetch(`/api/history?zone=${encodeURIComponent(zone)}&hours=${hours}`);
    const data = await res.json();
    const now = Date.now();
    const min = now - hours * 3600 * 1000;
    document.getElementById("history-title").textContent = data.name;

    const palette = [css("--ink"), css("--muted"), css("--zone-6")];  // room sensor, heater's own sensor, …
    const datasets = [];
    Object.entries(data.series).forEach(([source, s], i) => {
      const kind = source.split(":")[0];
      if (s.temp.length) datasets.push(lineSet(`${kind} temp`, s.temp, palette[i % palette.length]));
      if (s.floor_temp && s.floor_temp.length) datasets.push(lineSet(`floor temp`, s.floor_temp, css("--zone-5"), { borderWidth: 1.5 }));
      if (s.setpoint.length) datasets.push(lineSet(`setpoint`, s.setpoint, css("--frost"), { borderDash: [6, 4], borderWidth: 1.5, stepped: true, tension: 0 }));
      if (s.power_w.length) datasets.push(lineSet(`heater W`, s.power_w, css("--ember-soft"), { yAxisID: "power", stepped: true, fill: true, borderWidth: 0, tension: 0, order: 10 }));
    });
    const outdoor = data.outdoor_sensor.length ? data.outdoor_sensor : data.outdoor_forecast;
    if (outdoor.length) datasets.push(lineSet(data.outdoor_sensor.length ? "outdoor" : "outdoor (forecast)", outdoor, css("--zone-6"), { borderWidth: 1.5, borderDash: [2, 3] }));
    const temps = datasets.filter((d) => d.yAxisID !== "power").flatMap((d) => d.data.map((p) => p.y));
    const top = temps.length ? Math.max(...temps) + 0.5 : 25;
    if (data.fires.length) datasets.push({ label: "fire", type: "scatter", data: data.fires.map((f) => ({ x: f.ts, y: top })), pointStyle: "triangle", pointRadius: 7, backgroundColor: css("--ember") });

    new Chart(document.getElementById("history-chart"), {
      type: "line",
      data: { datasets },
      options: {
        ...baseOptions(),
        parsing: false,
        plugins: { tooltip: { callbacks: { title: (items) => new Date(items[0].parsed.x).toLocaleString("et-EE", { timeZone }) } } },
        scales: {
          x: timeAxis(min, now),
          y: { grace: 0.5, title: { display: true, text: "°C" } },
          power: { display: "auto", position: "right", beginAtZero: true, grid: { display: false }, title: { display: true, text: "W" } },
        },
      },
    });

    new Chart(document.getElementById("history-price-chart"), {
      type: "line",
      data: { datasets: [lineSet("c/kWh", data.prices, css("--ochre"), { stepped: true, tension: 0 })] },
      options: { ...baseOptions(), parsing: false, plugins: { legend: { display: false } }, scales: { x: timeAxis(min, now), y: { beginAtZero: true } } },
    });
  }

  const ZONE_COLORS = ["--zone-1", "--zone-2", "--zone-3", "--zone-4", "--zone-5", "--zone-6"];

  function renderEnergy(data) {
    const zones = data.devices.filter((d) => d.has_data).map((d) => d.zone);  // table order (JSON keys come sorted)
    const color = (zone) => css(ZONE_COLORS[data.devices.findIndex((d) => d.zone === zone) % ZONE_COLORS.length]);
    document.querySelectorAll(".swatch[data-zone]").forEach((el) => { el.style.background = color(el.dataset.zone); });
    const names = Object.fromEntries(data.devices.map((d) => [d.zone, d.name]));
    const label = (ms) => data.by_hour ? hhmm(ms) : dayDate(ms, true);
    let chart;
    function draw(unit) {
      if (chart) chart.destroy();
      chart = new Chart(document.getElementById("energy-chart"), {
        type: "bar",
        data: {
          labels: data.buckets.map((b) => label(b.ts * 1000)),
          datasets: zones.map((z) => ({ label: names[z], data: data.buckets.map((b) => b[unit][z]), backgroundColor: color(z), borderRadius: 2, stack: "s" })),
        },
        options: {
          ...baseOptions(),
          plugins: { tooltip: { callbacks: { label: (i) => `${i.dataset.label}: ${unit === "kwh" ? i.raw.toFixed(2) + " kWh" : i.raw.toFixed(2) + " €"}` } } },
          scales: { x: { stacked: true, grid: { display: false } }, y: { stacked: true, beginAtZero: true, title: { display: true, text: unit === "kwh" ? "kWh" : "€" } } },
        },
      });
    }
    draw("kwh");
    document.querySelectorAll("[data-unit]").forEach((btn) => btn.addEventListener("click", () => {
      document.querySelectorAll("[data-unit]").forEach((b) => { b.classList.toggle("on", b === btn); b.setAttribute("aria-pressed", b === btn); });
      draw(btn.dataset.unit);
    }));
  }

  document.addEventListener("DOMContentLoaded", () => {
    const energyData = document.getElementById("energy-data");
    if (energyData) renderEnergy(JSON.parse(energyData.textContent));
    const priceData = document.getElementById("price-data");
    if (priceData) renderPrices(document.getElementById("price-chart"), JSON.parse(priceData.textContent));
    if (window.HISTORY) renderHistory(window.HISTORY);
  });
})();
