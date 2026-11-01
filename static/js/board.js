/*
 * Thin wrapper over chessboard.js shared by the trainer, Puzzle Rush, live
 * play and the homepage puzzle.
 *
 * The big win is reuse: chessboard.js's destroy()+Chessboard() rebuilds all
 * 64 squares and re-decodes every piece image, which is what made each new
 * puzzle visibly stutter. SynapBoard.create() builds a board once; callers
 * then call show(fen, orientation) to jump straight to a new position.
 *
 * Boards also accept tap-to-move (tap a piece, then tap where it goes),
 * which is far easier than dragging on a phone. Pass opts.legalMoves(square)
 * returning the target squares for a piece to get dots on those squares.
 * Either way the move goes through the same opts.onDrop(from, to) handler,
 * which must update the board position itself when it accepts a move.
 */
(function (global) {
  'use strict';

  var PIECES = '/static/img/chesspieces/wikipedia/{piece}.png';

  // Warm the image cache so the first board paints with its pieces.
  ['wK', 'wQ', 'wR', 'wB', 'wN', 'wP', 'bK', 'bQ', 'bR', 'bB', 'bN', 'bP'].forEach(function (p) {
    var img = new Image();
    img.src = PIECES.replace('{piece}', p);
  });

  function create(elementId, opts) {
    opts = opts || {};
    var el = document.getElementById(elementId);
    var selected = null;
    var hinted = [];
    var board = null;

    function squareEl(sq) { return el.querySelector('.square-' + sq); }
    function clearSelection() {
      if (selected && squareEl(selected)) squareEl(selected).classList.remove('sq-sel');
      hinted.forEach(function (n) { n.classList.remove('sq-hint', 'sq-hint-capture'); });
      hinted = [];
      selected = null;
    }
    function select(sq) {
      clearSelection();
      var targets = opts.legalMoves ? opts.legalMoves(sq) : null;
      if (targets && !targets.length) return;
      selected = sq;
      squareEl(sq).classList.add('sq-sel');
      var pos = board.position();
      (targets || []).forEach(function (t) {
        var n = squareEl(t);
        if (!n) return;
        n.classList.add(pos[t] ? 'sq-hint-capture' : 'sq-hint');
        hinted.push(n);
      });
    }
    function tryMove(from, to) {
      clearSelection();
      if (opts.onDragStart && opts.onDragStart(from, board.position()[from]) === false) return;
      if (opts.onDrop) opts.onDrop(from, to);
    }

    // A tap on a piece arrives as a "drag" dropped on its own square. Phones
    // then replay the same tap as mouse events, which chessboard.js also
    // listens for, so a second same-square drop right after the first is
    // ignored rather than undoing the selection.
    var lastTap = { sq: null, at: 0 };
    function onDrop(source, target, piece) {
      if (target === source) {
        var now = Date.now();
        if (lastTap.sq === source && now - lastTap.at < 600) return 'snapback';
        lastTap = { sq: source, at: now };
        if (selected && selected !== source) {
          // Tapping another piece that has moves of its own switches to it,
          // unless it can be captured by the selected piece.
          var fromHere = opts.legalMoves ? opts.legalMoves(selected) : null;
          var switchTo = fromHere && fromHere.indexOf(source) === -1 && opts.legalMoves(source).length;
          if (switchTo) select(source); else tryMove(selected, source);
        }
        else if (selected === source) clearSelection();
        else select(source);
        return 'snapback';
      }
      clearSelection();
      return opts.onDrop ? opts.onDrop(source, target, piece) : undefined;
    }

    // A tap on an empty square is a plain click. So is a tap on a piece the
    // page won't let anyone pick up (an enemy piece to capture), since no
    // drag starts there. Taps that did start a drag were already handled by
    // onDrop above, which clears the selection before this click arrives.
    el.addEventListener('click', function (e) {
      if (!selected) return;
      var node = e.target.closest('[data-square]');
      if (!node) return;
      var sq = node.getAttribute('data-square');
      if (sq === selected) return;
      tryMove(selected, sq);
    });

    board = global.Chessboard(elementId, {
      position:      opts.position || 'start',
      orientation:   opts.orientation || 'white',
      draggable:     opts.draggable !== false,
      pieceTheme:    PIECES,
      showNotation:  opts.showNotation !== false,
      moveSpeed:     140,
      snapbackSpeed: 90,
      snapSpeed:     40,
      appearSpeed:   80,
      trashSpeed:    80,
      onDragStart:   opts.onDragStart,
      onDrop:        onDrop,
      onSnapEnd:     opts.onSnapEnd
    });

    var lastMarked = [];
    function clearMarks() {
      lastMarked.forEach(function (sq) { sq.classList.remove('sq-last'); });
      lastMarked = [];
    }

    var api = {
      raw: board,
      position: function (fen, animate) { board.position(fen, animate !== false); },
      fen: function () { return board.fen(); },
      // Jump to a fresh position with no animation (new puzzle / new game).
      show: function (fen, orientation) {
        clearMarks();
        clearSelection();
        if (orientation && board.orientation() !== orientation) board.orientation(orientation);
        board.position(fen, false);
      },
      orientation: function (o) { return o ? board.orientation(o) : board.orientation(); },
      markMove: function (from, to) {
        clearMarks();
        [from, to].forEach(function (sq) {
          var node = el.querySelector('.square-' + sq);
          if (node) { node.classList.add('sq-last'); lastMarked.push(node); }
        });
      },
      clearMarks: clearMarks,
      clearSelection: clearSelection,
      flashWrong: function () {
        el.classList.remove('is-wrong');
        void el.offsetWidth;
        el.classList.add('is-wrong');
      },
      resize: function () { board.resize(); }
    };

    var resizeTimer = null;
    global.addEventListener('resize', function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () { board.resize(); }, 80);
    });
    return api;
  }

  global.SynapBoard = { create: create, PIECES: PIECES };
})(window);
