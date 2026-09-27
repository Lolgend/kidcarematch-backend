import { BrowserRouter, Routes, Route } from "react-router-dom";
import LoginPage from "./pages/LoginPage";
import RegisterPage from "./pages/RegisterPage";
import PasswordResetPage from "./pages/PasswordResetPage";
import PreferenceProfilePage from "./pages/PreferenceProfilePage";
import DirectoryPage from "./pages/DirectoryPage";
import CentreDetailsPage from "./pages/CentreDetailsPage";
import RankedMatchesPage from "./pages/RankedMatchesPage";
import ComparisonPage from "./pages/ComparisonPage";
import WatchlistPage from "./pages/WatchlistPage";
import ReviewPage from "./pages/ReviewPage";
import AdminUserManagementPage from "./pages/AdminUserManagementPage";
import AdminReviewModerationPage from "./pages/AdminReviewModerationPage";
import AdminSubsidyConfigPage from "./pages/AdminSubsidyConfigPage";
import AdminDataSourceDashboardPage from "./pages/AdminDataSourceDashboardPage";
import AdminEditProfilePage from "./pages/AdminEditProfilePage";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<DirectoryPage />} />
        <Route path="/login" element={<LoginPage />} />
        <Route path="/register" element={<RegisterPage />} />
        <Route path="/reset-password" element={<PasswordResetPage />} />
        <Route path="/preferences" element={<PreferenceProfilePage />} />
        <Route path="/centres/:centreCode" element={<CentreDetailsPage />} />
        <Route path="/matches" element={<RankedMatchesPage />} />
        <Route path="/compare" element={<ComparisonPage />} />
        <Route path="/watchlist" element={<WatchlistPage />} />
        <Route path="/reviews" element={<ReviewPage />} />
        <Route path="/admin/users" element={<AdminUserManagementPage />} />
        <Route path="/admin/reviews" element={<AdminReviewModerationPage />} />
        <Route path="/admin/subsidy" element={<AdminSubsidyConfigPage />} />
        <Route path="/admin/data-sources" element={<AdminDataSourceDashboardPage />} />
        <Route path="/admin/profile" element={<AdminEditProfilePage />} />
      </Routes>
    </BrowserRouter>
  );
}
