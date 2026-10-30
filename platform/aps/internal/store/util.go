package store

import "strconv"

func atoi(s string) int {
	n, _ := strconv.Atoi(s)
	return n
}

func itoa(n int) string { return strconv.Itoa(n) }
