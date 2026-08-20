"use client";

import type { Board } from "@workspace/api-client";
import type { Dispatch, SetStateAction } from "react";
import { api } from "../api";
import { describeError } from "../views/shared";

export type BoardHandlerDeps = {
  setError: Dispatch<SetStateAction<string>>;
  setBoards: Dispatch<SetStateAction<Board[]>>;
};

export function createBoardHandlers({ setError, setBoards }: BoardHandlerDeps) {
  async function createBoard(name: string) {
    setError("");
    try {
      const created = await api.createBoard(name);
      setBoards((items) => [...items, created]);
    } catch (caught) {
      setError(describeError(caught, "Could not create that board"));
    }
  }

  function replaceBoard(board: Board) {
    setBoards((items) => items.map((item) => (item.id === board.id ? board : item)));
  }

  async function addBoardCard(boardId: string, column: string, title: string) {
    try {
      replaceBoard(await api.addBoardCard(boardId, column, title));
    } catch (caught) {
      setError(describeError(caught, "Could not add that card"));
    }
  }

  async function moveBoardCard(boardId: string, cardId: string, column: string) {
    try {
      replaceBoard(await api.moveBoardCard(boardId, cardId, column));
    } catch (caught) {
      setError(describeError(caught, "Could not move that card"));
    }
  }

  async function removeBoardCard(boardId: string, cardId: string) {
    try {
      replaceBoard(await api.deleteBoardCard(boardId, cardId));
    } catch (caught) {
      setError(describeError(caught, "Could not delete that card"));
    }
  }

  async function removeBoard(board: Board) {
    // A board takes its cards with it, which is more than the button says.
    if (!window.confirm(`Delete “${board.name}” and its cards?`)) return;
    try {
      await api.deleteBoard(board.id);
      setBoards((items) => items.filter((item) => item.id !== board.id));
    } catch (caught) {
      setError(describeError(caught, "Could not delete that board"));
    }
  }

  const boardColumnOps = {
    addColumn: async (boardId: string, name: string, index?: number) => {
      try {
        replaceBoard(await api.addBoardColumn(boardId, name, index));
      } catch (caught) {
        setError(describeError(caught, "Could not add that column"));
      }
    },
    renameColumn: async (boardId: string, columnId: string, name: string) => {
      try {
        replaceBoard(await api.renameBoardColumn(boardId, columnId, name));
      } catch (caught) {
        setError(describeError(caught, "Could not rename that column"));
      }
    },
    deleteColumn: async (boardId: string, columnId: string, moveCardsTo?: string) => {
      try {
        replaceBoard(await api.deleteBoardColumn(boardId, columnId, moveCardsTo));
      } catch (caught) {
        setError(describeError(caught, "Could not delete that column"));
      }
    },
    reorderColumns: async (boardId: string, order: string[]) => {
      try {
        replaceBoard(await api.reorderBoardColumns(boardId, order));
      } catch (caught) {
        setError(describeError(caught, "Could not reorder the columns"));
      }
    },
    reorderCard: async (
      boardId: string,
      cardId: string,
      index: number,
      columnId?: string,
    ) => {
      try {
        replaceBoard(await api.reorderBoardCard(boardId, cardId, index, columnId ?? ""));
      } catch (caught) {
        setError(describeError(caught, "Could not move that card"));
      }
    },
  };

  return {
    createBoard,
    addBoardCard,
    moveBoardCard,
    removeBoardCard,
    removeBoard,
    boardColumnOps,
  };
}
