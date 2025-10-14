<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Print</title>

  <style>
    /* Make the physical page exactly the width you pass in (no margins) */
    @page {
      size: {{ label_mm|default(72) }}mm auto;
      margin: 0;
    }

    /* Reset browser margins in print and screen so it matches */
    html, body {
      margin: 0;
      padding: 0;
      background: #fff;
    }

    /* Ticket container equals the paper width */
    .ticket {
      width: {{ label_mm|default(72) }}mm;
      margin: 0 auto;
      padding: 0;         /* no side padding so we use full width */
    }

    /* The invoice text block */
    pre {
      margin: 0;
      padding: 0;
      white-space: pre;   /* do not wrap the monospace lines */
      line-height: 1.2;
      /* TUNE this to fill the width visually on your printer */
      font-family: "Courier New", Courier, monospace;
      font-size: 16px;    /* try 15–17px for 72–80mm paper */
    }

    /* On screen, center and give a faint border for preview */
    @media screen {
      body { padding: 10px; background:#f5f5f5; }
      .ticket { background:#fff; border:1px solid #eee; box-shadow:0 1px 4px rgba(0,0,0,.06); }
      pre { padding: 8px 6px; }
    }

    /* In print, remove any padding so it uses full width */
    @media print {
      body { background:#fff; }
      .ticket { border: none; box-shadow: none; }
      pre { padding: 0; }
    }
  </style>
</head>
<body>
  <div class="ticket">
    <pre>{{ invoice|safe }}</pre>
  </div>

  <script>
    // Optional: auto-open print dialog when loaded from /print route
    if (window.matchMedia) {
      const isPrintView = /\/print(\/|$|\?)/.test(location.pathname);
      if (isPrintView) setTimeout(() => window.print(), 100);
    }
  </script>
</body>
</html>