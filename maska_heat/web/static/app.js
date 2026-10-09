// Charts for the dashboard and history pages. Times arrive as unix ms; shown in the browser's local time.
(function () {
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const hhmm = (ms) => new Date(ms).toLocaleTimeString("et-EE", { hour: "2-digit", minute: "2-digit" });
  const dayHour = (ms) => {
    const d = new Date(ms);
    return d.toLocaleDateString("en-GB", { weekday: "short" }) + " " + String(d.getHours()).padStart(2, "0");
  };

  function baseOptions() {
    Chart.defaults.color = css("--muted");
    Chart.defaults.borderColor = css("--line");
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    return { responsive: true, maintainAspectRatio: false, animation: false, interaction: { mode: "index", intersect: false } };
  }

  function renderPrices(canvas, slots) {
    const colors = slots.map((s) => (s.is_now ? css("--now") : s.is_day ? css("--day") : css("--night")));
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
              title: (items) => dayHour(+items[0].label) + ":00",
              label: (item) => {
                const s = slots[item.dataIndex];
                return [`${item.raw.toFixed(1)} c/kWh total`, `spot ${(s.eur_mwh / 10).toFixed(1)} c/kWh`, s.is_day ? "day tariff" : "night tariff"];
              },
            },
          },
        },
        scales: {
          x: { ticks: { callback: function (v) { const ms = +this.getLabelForValue(v); return new Date(ms).getHours() % 3 === 0 ? dayHour(ms) : ""; }, autoSkip: false, maxRotation: 0 }, grid: { display: false } },
          y: { beginAtZero: true, title: { display: true, text: "c/kWh" } },
        },
      },
    });
  }

  function lineSet(label, points, color, extra = {}) {
    return { label, data: points.map(([x, y]) => ({ x, y })), borderColor: color, backgroundColor: color, pointRadius: 0, borderWidth: 2, tension: 0.2, ...extra };
  }

  function timeAxis(min, max) {
    return { type: "linear", min, max, ticks: { callback: (v) => { const d = new Date(v); return d.getHours() === 0 && d.getMinutes() === 0 ? d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric" }) : hhmm(v); }, maxTicksLimit: 10 } };
  }

  async function renderHistory({ zone, hours }) {
    const res = await fetch(`/api/history?zone=${encodeURIComponent(zone)}&hours=${hours}`);
    const data = await res.json();
    const now = Date.now();
    const min = now - hours * 3600 * 1000;
    document.getElementById("history-title").textContent = data.name;

    const palette = [css("--accent"), css("--text"), css("--day")];
    const datasets = [];
    Object.entries(data.series).forEach(([source, s], i) => {
      const kind = source.split(":")[0];
      if (s.temp.length) datasets.push(lineSet(`${kind} temp`, s.temp, palette[i % palette.length]));
      if (s.setpoint.length) datasets.push(lineSet(`setpoint`, s.setpoint, css("--cool"), { borderDash: [6, 4], borderWidth: 1.5, stepped: true, tension: 0 }));
      if (s.power_w.length) datasets.push(lineSet(`heater W`, s.power_w, css("--accent-soft"), { yAxisID: "power", stepped: true, fill: true, borderWidth: 0, tension: 0, order: 10 }));
    });
    const outdoor = data.outdoor_sensor.length ? data.outdoor_sensor : data.outdoor_forecast;
    if (outdoor.length) datasets.push(lineSet(data.outdoor_sensor.length ? "outdoor" : "outdoor (forecast)", outdoor, css("--night"), { borderWidth: 1.5 }));
    const temps = datasets.filter((d) => d.yAxisID !== "power").flatMap((d) => d.data.map((p) => p.y));
    const top = temps.length ? Math.max(...temps) + 0.5 : 25;
    if (data.fires.length) datasets.push({ label: "fire", type: "scatter", data: data.fires.map((f) => ({ x: f.ts, y: top })), pointStyle: "triangle", pointRadius: 7, backgroundColor: css("--accent") });

    new Chart(document.getElementById("history-chart"), {
      type: "line",
      data: { datasets },
      options: {
        ...baseOptions(),
        parsing: false,
        plugins: { tooltip: { callbacks: { title: (items) => new Date(items[0].parsed.x).toLocaleString("et-EE") } } },
        scales: {
          x: timeAxis(min, now),
          y: { grace: 0.5, title: { display: true, text: "°C" } },
          power: { display: "auto", position: "right", beginAtZero: true, grid: { display: false }, title: { display: true, text: "W" } },
        },
      },
    });

    new Chart(document.getElementById("history-price-chart"), {
      type: "line",
      data: { datasets: [lineSet("c/kWh", data.prices, css("--day"), { stepped: true, tension: 0 })] },
      options: { ...baseOptions(), parsing: false, plugins: { legend: { display: false } }, scales: { x: timeAxis(min, now), y: { beginAtZero: true } } },
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    const priceData = document.getElementById("price-data");
    if (priceData) renderPrices(document.getElementById("price-chart"), JSON.parse(priceData.textContent));
    if (window.HISTORY) renderHistory(window.HISTORY);
  });
})();
